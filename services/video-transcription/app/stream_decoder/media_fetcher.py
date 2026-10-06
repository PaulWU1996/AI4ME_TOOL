"""
DASH Media Segment Downloader: Download DASH media segments for a given time range.
"""

import logging
import queue
import threading
from typing import Optional, List, Tuple, Dict
from dataclasses import dataclass
from time import sleep  
import xml.etree.ElementTree as ET
import os
from pathlib import Path
from collections import deque

import requests
from .media_selector import request_programme

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("MediaFetcher")


@dataclass
class DASHSegment:
    """Represents a single DASH media segment."""
    url: str
    start_time_ms: int
    duration_ms: int
    index: int
    representation_id: Optional[str] = None
    init_segment_url: Optional[str] = None  # URL for initialization segment, if applicable
    
    def end_time_ms(self) -> int:
        """Calculate the end time of this segment."""
        return self.start_time_ms + self.duration_ms
    
    def covers_time(self, time_ms: int) -> bool:
        """Check if this segment covers the given time."""
        return self.start_time_ms <= time_ms < self.end_time_ms()


class DASHRepresentation:
    def __init__(self, id: str, bandwidth: int, width: Optional[int] = None, height: Optional[int] = None, frame_rate: Optional[int] = None, codecs: Optional[str] = None, scanType: Optional[str] = None):
        self.id = id
        self.media_type = "video"  if id.startswith("video") else "audio"
        self.bandwidth = bandwidth
        self.width = width
        self.height = height
        self.frame_rate = frame_rate
        self.codecs = codecs
        self.scanType = scanType

    def __init__(self, element):
        self.id = element.get("id")
        self.media_type = "video"  if self.id.startswith("video") else "audio"
        self.bandwidth = int(element.get("bandwidth", 0))
        self.width = int(element.get("width", 0)) if element.get("width") else None
        self.height = int(element.get("height", 0)) if element.get("height") else None
        self.frame_rate = int(element.get("frameRate", 0)) if element.get("frameRate") else None
        self.codecs = element.get("codecs")
        self.scanType = element.get("scanType")

    def is_video(self) -> bool:
        return self.media_type.lower() == "video"

    def is_audio(self) -> bool:
        return self.media_type.lower() == "audio"



class DashStreamBuffer:
    """Thread-safe streaming buffer for DASH segments.
    
    Allows one writer thread to add segments via write() while a decoder thread
    reads from the buffer. Uses a queue for thread-safe segment arrival and a lock
    to protect the internal buffer state.
    """
    
    def __init__(self, max_size: int):
        self.queue = queue.Queue(max_size)
        self.chunks = deque()  # Queue of byte chunks
        self.chunk_offset = 0  # Offset into the first chunk
        self.eof = False
        self.lock = threading.Lock()  # Protects chunks, chunk_offset, and eof

    def __del__(self):
        self.eof = True
        self.queue.queue.clear()
        with self.lock:
            self.chunks.clear()
            self.chunk_offset = 0


    def write(self, chunk: bytes) -> bool:
        """Writer thread calls this to add segment data."""
        try:
            self.queue.put(chunk, timeout=3.0)
            logger.info("Chunk written to DashStreamBuffer successfully.")
            return True
        except queue.Full:
            logger.warning("Queue is full.")
            return False

    def _buffered_size_unlocked(self) -> int:
        """Calculate total buffered data size. Must be called with lock held."""
        if not self.chunks:
            return 0
        total = sum(len(c) for c in self.chunks) - self.chunk_offset
        return total

    def read(self, size: int) -> bytes:
        """Decoder thread calls this (via PyAV) to read buffered data."""
        # Fill buffer from queue until we have enough or EOF
        while True:
            # Check buffered size and EOF status under lock
            with self.lock:
                buffered = self._buffered_size_unlocked()
                if buffered >= size or self.eof:
                    break
            
            # Don't hold lock during blocking queue.get()
            try:
                chunk = self.queue.get(timeout=0.5)
            except queue.Empty:
                logger.warning("Download queue is empty.")
                with self.lock:
                    if self.eof:
                        logger.info("EOF reached while queue is empty.")
                        break
                continue
            
            if chunk is None:
                logger.info("Chunk is None ('None' was written to the queue), signaling EOF.")
                with self.lock:
                    self.eof = True
                break
            
            # Append chunk under lock
            with self.lock:
                self.chunks.append(chunk)

        # Read data from buffer (single lock hold for all buffer operations)
        with self.lock:
            data = bytearray()
            bytes_needed = size
            
            while bytes_needed > 0 and len(self.chunks) > 0:
                first_chunk = self.chunks[0]
                available = len(first_chunk) - self.chunk_offset
                to_read = min(bytes_needed, available)
                
                data.extend(first_chunk[self.chunk_offset:self.chunk_offset + to_read])
                bytes_needed -= to_read
                self.chunk_offset += to_read
                
                # Move to next chunk if current is exhausted
                if self.chunk_offset >= len(first_chunk):
                    self.chunks.popleft()  # O(1) operation
                    self.chunk_offset = 0
        
        return bytes(data)

    def flush(self):
        """Flush the buffer by clearing all chunks."""

        # append all remaining queue items to the deque before clearing
        while True:
            try:
                chunk = self.queue.get_nowait()
                if chunk is None:
                    break
                with self.lock:
                    self.chunks.append(chunk)
            except queue.Empty:
                break     


    def close(self):
        """Call this when writing is complete. Signals EOF to reader."""
        try:
            # Try to put None with short timeout, don't block indefinitely
            self.flush()
            self.queue.put_nowait(None)
            self.eof = True
        except queue.Full:
            logger.warning("Queue full, could not write EOF marker")


    def seekable(self) -> bool:
        return False


    def get_size(self) -> int:
        """Return the number of chunks currently in the buffer."""
        with self.lock:
            return len(self.chunks)




class DASHManifest:
    """
    The DASH Media Presentation Description for one programme.

    Each DASHMediaFetcher owns one of these and fetches it in init().
    """

    def __init__(self, programme_id: str | None = None):
        """
        Initialize the manifest.

        Nothing is fetched here. Call fetch() to resolve the manifest URL and download
        the MPD; it is safe to call concurrently and only requests once.

        Args:
            programme_id: BBC programme ID (e.g., 'm002vqlg')
        """
        self.programme_id = programme_id
        self.stream_url: str | None = None
        self.mpd_data: str | None = None
        self.base_url: str | None = None
        self._fetch_lock = threading.Lock()

    def fetch(self) -> bool:
        """
        Resolve the manifest URL and download the MPD.

        Returns:
            True if the manifest is available, False otherwise
        """
        with self._fetch_lock:
            if self.mpd_data:
                return True
            
            if not self.programme_id:
                logger.error("Programme ID not set; cannot fetch manifest")
                return False
            
            self.stream_url = request_programme(self.programme_id)
            if not self.stream_url:
                logger.error(
                    f"Could not fetch stream URL for programme {self.programme_id}"
                )
                return False

            logger.info(f"Stream URL obtained: {self.stream_url}\n")

            # Base URL for relative segment URLs
            self.base_url = '/'.join(self.stream_url.split('/')[:-1])

            try:
                logger.info("Fetching DASH MPD...")
                response = requests.get(self.stream_url, timeout=10)
                response.raise_for_status()

                self.mpd_data = response.text
                logger.info("MPD fetched successfully")

                logger.debug(
                    f"MPD data (first 2000 chars): {self.mpd_data[:2000]}..."
                )
                return True

            except requests.RequestException as e:
                logger.error(f"Failed to fetch MPD: {e}")
                return False

    @property
    def is_available(self) -> bool:
        """Whether the MPD has been fetched."""
        return bool(self.mpd_data)


class DASHMediaFetcher:
    """
    Fetches and downloads DASH media segments for a given programme and time range.
    """
    
    def __init__(self, programme_id: str):
        """
        Initialize the DASH Media Fetcher.

        Args:
            programme_id: BBC programme ID
        """
        self.manifest = DASHManifest(programme_id)
        self.segments: List[DASHSegment] = []
        self.thread = None
        self._stop_event = threading.Event()  # Stop signal for fetcher thread
        self.selected_representation = None
        self.media_type: None | str = None
        self.image_width: None | int = None
        self.image_height: None | int = None
        self.audio_bitrate: None | int = None


    def init(
        self,
        media_type: str,
        quality: dict,
        rep_selected_func,
    ) -> bool:
        """
        Fetch the MPD, select this fetcher's representation and parse its segments.

        Args:
            media_type: Type of media ('video' or 'audio')
            quality: Dictionary specifying quality parameters (e.g., {'width': 1920, 'height': 1080} for video or {'bitrate': 128000} for audio)
            rep_selected_func: Callback function to be called with the selected representation

        Returns:
            True if the fetcher is ready to download segments
        """
        self.media_type = media_type
        if media_type not in ["video", "audio"]:
            raise ValueError(f"Unsupported media type: {media_type}")

        if media_type == "video":
            self.image_width = quality.get("width")
            self.image_height = quality.get("height")
            if not self.image_width or not self.image_height:
                raise ValueError("Quality must be specified for video media type")

        if media_type == "audio":
            self.audio_bitrate = quality.get("bitrate")
            if not self.audio_bitrate:
                raise ValueError("Quality must be specified for audio media type")

        if not self.manifest.fetch():
            logger.error(
                f"Failed to fetch manifest for programme {self.manifest.programme_id}"
            )
            return False

        # Parse MPD for segments info and select representation
        self.segments = self.parse_segments()
        if len(self.segments) == 0:
            logger.error(
                f"No segments found for programme {self.manifest.programme_id}"
            )
            return False

        rep_selected_func(self.selected_representation)
        return True

    def _select_representation(self, representations: List[ET.Element]) -> ET.Element | None:
        """
        Select the representation matching this fetcher's media type and quality.

        Candidates are expected to already be restricted to this fetcher's
        media_type by parse_segments. Video is chosen by the representation whose
        dimensions are closest to the requested width and height; audio by the
        representation whose bandwidth is closest to the requested bitrate.

        Args:
            representations: Candidate Representation elements

        Returns:
            Selected representation element, or None if there are no candidates
        """
        if not representations:
            return None

        if self.media_type == 'video':
            # Find representation closest to target dimensions
            best_distance = float('inf')
            best_rep = None

            for rep in representations:
                if not rep.get('width') or not rep.get('height'):
                    continue

                rep_width = int(rep.get('width', 0))
                rep_height = int(rep.get('height', 0))

                # Euclidean distance from target dimensions
                distance = ((rep_width - (self.image_width or 0)) ** 2 +
                        (rep_height - (self.image_height or 0)) ** 2) ** 0.5

                if distance < best_distance:
                    best_distance = distance
                    best_rep = rep
                    logger.info(f"Found representation: {rep.get('id')}, "
                            f"dimensions: {rep_width}x{rep_height}, distance: {distance:.2f}")

            if best_rep:
                logger.info(f"Selected representation: {best_rep.get('id')}")
                return best_rep

            # fall back to the highest bandwidth candidate
            best_rep = max(representations, key=lambda r: int(r.get('bandwidth', 0)))
            logger.info(
                f"No representation declared dimensions, falling back to "
                f"highest bandwidth: {best_rep.get('id')}"
            )
            return best_rep
        
        if self.media_type == 'audio':
            # select representation closest to target bitrate
            best_rep = min(
                representations,
                key=lambda r: abs(int(r.get('bandwidth', 0)) - (self.audio_bitrate or 0)),
            )
            logger.info(f"Selected audio representation: {best_rep.get('id')}")
            return best_rep

        return None


    def parse_segments(self) -> List[DASHSegment]:
        """
        Parse DASH MPD and extract segment information for this fetcher's media type.

        Only AdaptationSets matching self.media_type are considered. Audio and video
        are separate Representations of one MPD, each with its own init segment.

        Returns:
            List of DASHSegment objects
        """
        if not self.manifest.mpd_data:
            logger.error("MPD data not available on the manifest. Call init() first.")
            return []
        
        segments = []
        
        try:
            root = ET.fromstring(self.manifest.mpd_data)
            
            # DASH namespace
            ns = {'dash': 'urn:mpeg:dash:schema:mpd:2011'}

            candidates = []
            
            # Find representations belonging to this fetcher's media type, keeping each
            # paired with its AdaptationSet because SegmentList and SegmentTemplate may
            # be declared there and inherited by the Representation.
            for period in root.findall('.//dash:Period', ns):
                for adaptation_set in period.findall('.//dash:AdaptationSet', ns):
                    if not self._is_media_type(adaptation_set):
                        continue
                    for rep in adaptation_set.findall('.//dash:Representation', ns):
                        logger.info(f"Parsing representation ID: {rep.get('id')}")
                        logger.info(f"Representation attributes: {rep.attrib}")
                        candidates.append((rep, adaptation_set))

            if not candidates:
                logger.warning(
                    f"No '{self.media_type}' representations found in MPD"
                )
                return []
            
            # Sort by bandwidth descending so quality selection can fall back to the
            # highest bandwidth representation when nothing matches more precisely.
            candidates.sort(
                key=lambda pair: int(pair[0].get('bandwidth', 0)), reverse=True
            )

            representations = [pair[0] for pair in candidates]
            best_rep = self._select_representation(representations)
            if best_rep is None:
                logger.warning("No representation selected")
                return []

            self.selected_representation = DASHRepresentation(best_rep)

            # Recover the AdaptationSet the winner came from, for segment inheritance
            adaptation_set = next(
                pair[1] for pair in candidates if pair[0] is best_rep
            )

            # Extract segment information
            segments.extend(
                self._extract_segments_from_representation(
                    best_rep, ns, adaptation_set
                )
            )
            logger.info(f"Parsed {len(segments)} segments from MPD")
            self.segments = sorted(segments, key=lambda s: s.start_time_ms)
            
            return self.segments
            
        except ET.ParseError as e:
            logger.error(f"Failed to parse MPD XML: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error parsing segments: {e}")
            return []

    def _is_media_type(self, adaptation_set: ET.Element) -> bool:
        """
        Check whether an AdaptationSet belongs to this fetcher's media type.

        Prefers the contentType attribute, falling back to mimeType for manifests
        that omit contentType.

        Args:
            adaptation_set: XML AdaptationSet element

        Returns:
            True if this AdaptationSet carries the fetcher's media type
        """
        content_type = adaptation_set.get('contentType')
        if content_type is None:
            mime_type = adaptation_set.get('mimeType', '')
            content_type = mime_type.split('/')[0] if mime_type else None
        return content_type == self.media_type

    def _find_segment_source(
        self,
        representation: ET.Element,
        ns: Dict[str, str],
        adaptation_set: Optional[ET.Element] = None,
    ) -> Tuple[Optional[ET.Element], Optional[ET.Element]]:
        """
        Locate the SegmentList or SegmentTemplate governing a representation.

        DASH allows SegmentList and SegmentTemplate to be declared on the
        AdaptationSet and inherited by its Representations. BBC declares video
        segments at the Representation level but audio segments on the
        AdaptationSet, so both levels must be searched.

        Args:
            representation: XML Representation element
            ns: XML namespaces
            adaptation_set: Parent AdaptationSet, used to resolve inherited
                SegmentList or SegmentTemplate declarations

        Returns:
            Tuple of (segment_list, segment_template), either of which may be None
        """
        segment_list = representation.find('.//dash:SegmentList', ns)
        segment_template = representation.find('.//dash:SegmentTemplate', ns)

        if segment_list is None and segment_template is None and adaptation_set is not None:
            segment_list = adaptation_set.find('.//dash:SegmentList', ns)
            segment_template = adaptation_set.find('.//dash:SegmentTemplate', ns)
            if segment_list is not None or segment_template is not None:
                logger.info(
                    "Using AdaptationSet-level segment descriptor inherited by "
                    f"representation {representation.get('id')}"
                )

        return segment_list, segment_template

    def _extract_segments_from_representation(
        self, 
        representation: ET.Element, 
        ns: Dict[str, str],
        adaptation_set: Optional[ET.Element] = None,
    ) -> List[DASHSegment]:
        """
        Extract segment information from a representation element.
        
        Args:
            representation: XML representation element
            ns: XML namespaces
            adaptation_set: Parent AdaptationSet, used to resolve inherited
                SegmentList/SegmentTemplate declarations
            
        Returns:
            List of DASHSegment objects
        """
        representation_id = representation.get('id')
        segments = []
        
        segment_list, segment_template = self._find_segment_source(
            representation, ns, adaptation_set
        )
        
        # Check for SegmentList (explicit segment listing)
        if segment_list is not None:
            segments.extend(self._parse_segment_list(representation_id, segment_list, ns))
        
        # Check for SegmentTemplate (pattern-based segments)
        if segment_template is not None:
            segments.extend(self._parse_segment_template(representation_id, segment_template, ns))
        
        return segments
    
    def _parse_segment_list(
        self,
        representation_id: Optional[str],
        segment_list: ET.Element, 
        ns: Dict[str, str]
    ) -> List[DASHSegment]:
        """Parse segments from SegmentList element."""
        segments = []
        current_time = 0
        index = 0
        
        # Get timescale (default 1000 = milliseconds)
        timescale = int(segment_list.get('timescale', 1000))
        
        for segment_url in segment_list.findall('.//dash:SegmentURL', ns):
            media = segment_url.get('media', '')
            duration_attr = segment_url.get('duration', 0)
            
            if not media:
                continue
            
            duration_ms = int(duration_attr) * 1000 // timescale
            
            segment = DASHSegment(
                url=self._resolve_url(media),
                start_time_ms=current_time,
                duration_ms=duration_ms,
                index=index,
                representation_id=representation_id
            )
            segments.append(segment)
            
            current_time += duration_ms
            index += 1
        
        return segments
    
    def _parse_segment_template(
        self,
        representation_id: Optional[str],
        segment_template: ET.Element,
        ns: Dict[str, str]
    ) -> List[DASHSegment]:
        """Parse segments from SegmentTemplate element."""
        segments = []
        
        # Get media template pattern
        media_template = segment_template.get('media')
        if not media_template:
            return segments
        
        # Get initialization template
        init_segment_url = None
        init_template = segment_template.get('initialization')
        if init_template:
            init_segment_url = self._resolve_url(init_template.replace('$RepresentationID$', representation_id or ''))
        
        # Get timescale and segment duration
        timescale = int(segment_template.get('timescale', 1000))
        duration = int(segment_template.get('duration', 0))
        
        if duration == 0:
            return segments
        
        duration_ms = duration * 1000 // timescale
        
        # Check for SegmentTimeline (explicit timing info)
        segment_timeline = segment_template.find('.//dash:SegmentTimeline', ns)
        
        if segment_timeline is not None:
            # Use explicit timeline
            current_time = 0
            index = 0
            
            for s_elem in segment_timeline.findall('.//dash:S', ns):
                repeat = int(s_elem.get('r', 0)) + 1
                d = int(s_elem.get('d', duration))
                
                for _ in range(repeat):
                    segment_duration_ms = d * 1000 // timescale
                    media_url = media_template.replace('$Number$', str(index + 1))
                    media_url = media_url.replace('$Time$', str(current_time * timescale // 1000))
                    media_url = media_url.replace('$RepresentationID$', representation_id or '')
                    
                    segment = DASHSegment(
                        url=self._resolve_url(media_url),
                        start_time_ms=current_time,
                        duration_ms=segment_duration_ms,
                        index=index,
                        representation_id=representation_id,
                        init_segment_url=init_segment_url
                    )
                    segments.append(segment)
                    
                    current_time += segment_duration_ms
                    index += 1
        else:
            # No SegmentTimeline - generate segments based on presentation duration
            # Get total duration from the Period or MPD level
            total_duration_ms = self._get_total_duration_ms()

            logger.info(f"Total presentation duration: {total_duration_ms}ms")
            
            if total_duration_ms > 0:
                current_time = 0
                index = 0
                
                while current_time < total_duration_ms:
                    # Calculate how much of this segment fits in the total duration
                    segment_duration_ms = min(duration_ms, total_duration_ms - current_time)
                    
                    media_url = media_template.replace('$Number$', str(index + 1))
                    media_url = media_url.replace('$Time$', str(current_time * timescale // 1000))
                    media_url = media_url.replace('$RepresentationID$', representation_id or '')
                    
                    segment = DASHSegment(
                        url=self._resolve_url(media_url),
                        start_time_ms=current_time,
                        duration_ms=segment_duration_ms,
                        index=index,
                        representation_id=representation_id,
                        init_segment_url=init_segment_url          
                    )
                    segments.append(segment)
                    
                    current_time += segment_duration_ms
                    index += 1
        
        return segments
    
    def _get_total_duration_ms(self) -> int:
        """
        Extract total presentation duration from the MPD.
        
        Returns:
            Total duration in milliseconds, or 0 if not found
        """
        if not self.manifest.mpd_data:
            return 0
        
        try:
            root = ET.fromstring(self.manifest.mpd_data)
            
            # Try to get duration from MPD level
            duration_str = root.get('mediaPresentationDuration')
            if duration_str:
                return self._parse_iso8601_duration(duration_str)
            
            # Try to get duration from Period level
            ns = {'dash': 'urn:mpeg:dash:schema:mpd:2011'}
            for period in root.findall('.//dash:Period', ns):
                duration_str = period.get('duration')
                if duration_str:
                    return self._parse_iso8601_duration(duration_str)
            
            return 0
        except Exception as e:
            logger.warning(f"Could not extract total duration: {e}")
            return 0
    
    def _parse_iso8601_duration(self, duration_str: str) -> int:
        """
        Parse ISO 8601 duration string to milliseconds.
        
        Examples: PT20.053333S, PT1H30M45S, PT2H30M
        
        Args:
            duration_str: ISO 8601 duration string
            
        Returns:
            Duration in milliseconds
        """
        import re
        
        # Pattern: P[n]Y[n]M[n]DT[n]H[n]M[n]S
        pattern = r'P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?)?'
        match = re.match(pattern, duration_str)
        
        if not match:
            logger.warning(f"Could not parse duration: {duration_str}")
            return 0
        
        years, months, days, hours, minutes, seconds = match.groups()
        
        total_seconds = 0.0
        if years:
            total_seconds += int(years) * 365 * 24 * 3600
        if months:
            total_seconds += int(months) * 30 * 24 * 3600
        if days:
            total_seconds += int(days) * 24 * 3600
        if hours:
            total_seconds += int(hours) * 3600
        if minutes:
            total_seconds += int(minutes) * 60
        if seconds:
            total_seconds += float(seconds)
        
        return int(total_seconds * 1000)
    
    def _resolve_url(self, url: str) -> str:
        """
        Resolve relative URLs to absolute URLs.
        
        Args:
            url: Potentially relative URL
            
        Returns:
            Absolute URL
        """
        if url.startswith('http://') or url.startswith('https://'):
            return url
        
        base_url = self.manifest.base_url or ""

        if url.startswith('/'):
            # Absolute path - use domain from base URL
            parts = base_url.split('/')
            return f"{parts[0]}//{parts[2]}{url}"
        
        # Relative URL - append to base URL
        return f"{base_url}/{url}"
    
    def download_segments(
        self, 
        current_time_ms: int, 
        look_ahead_ms: int,
        output_dir: Optional[str] = None
    ) -> Tuple[List[DASHSegment], List[str]]:
        """
        Download DASH media segments for the specified time range.
        
        Args:
            current_time_ms: Start time in milliseconds
            look_ahead_ms: Duration to look ahead in milliseconds
            output_dir: Directory to save downloaded segments
                       If None, segments are not saved to disk
        
        Returns:
            Tuple of (downloaded_segments, downloaded_file_paths)
            
        Raises:
            RuntimeError: If MPD has not been fetched/parsed
        """
        if not self.segments:
            raise RuntimeError(
                "No segments available. Call init() first."
            )

        # Download initialization segment if available
        init_segment_downloaded = False
        if self.segments and self.segments[0].init_segment_url:
            init_segment_url = self.segments[0].init_segment_url
            try:
                logger.debug(f"Downloading initialization segment: {init_segment_url}")
                response = requests.get(init_segment_url, timeout=10)
                response.raise_for_status()
                
                if output_dir:
                    filename = "init_segment.m4s"
                    filepath = os.path.join(output_dir, filename)
                    with open(filepath, 'wb') as f:
                        f.write(response.content)
                    downloaded_files = [filepath]
                    logger.debug(f"Saved initialization segment to {filepath}")
                
                init_segment_downloaded = True
            except requests.RequestException as e:
                logger.error(f"Failed to download initialization segment: {e}")


        end_time_ms = current_time_ms + look_ahead_ms
        
        # Find segments that overlap with the requested time range
        relevant_segments = [
            seg for seg in self.segments
            if seg.start_time_ms < end_time_ms and seg.end_time_ms() > current_time_ms
        ]
        
        logger.info(
            f"Downloading segments for time range {current_time_ms}ms - {end_time_ms}ms"
        )
        logger.info(f"Found {len(relevant_segments)} relevant segments")
        
        downloaded_files = []
        
        if output_dir:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
        
        for segment in relevant_segments:
            try:
                logger.debug(f"Downloading segment {segment.index}: {segment.url}")
                logger.debug(f"segment start time: {segment.start_time_ms}ms, duration: {segment.duration_ms}ms")
                response = requests.get(segment.url, timeout=10)
                response.raise_for_status()
                
                if output_dir:
                    filename = f"segment_{segment.index:06d}.m4s"
                    filepath = os.path.join(output_dir, filename)
                    with open(filepath, 'wb') as f:
                        f.write(response.content)
                    downloaded_files.append(filepath)
                    logger.debug(f"Saved segment to {filepath}")
                
            except requests.RequestException as e:
                logger.error(f"Failed to download segment {segment.index}: {e}")
        
        logger.info(f"Downloaded {len(downloaded_files)} segment files")
        return relevant_segments, downloaded_files
    
    def get_segments_for_range(
        self, 
        current_time_ms: int, 
        look_ahead_ms: int
    ) -> List[DASHSegment]:
        """
        Get segments that cover a specific time range without downloading.
        
        Args:
            current_time_ms: Start time in milliseconds
            look_ahead_ms: Duration to look ahead in milliseconds
        
        Returns:
            List of DASHSegment objects covering the time range
        """
        end_time_ms = current_time_ms + look_ahead_ms
        
        relevant_segments = [
            seg for seg in self.segments
            if seg.start_time_ms < end_time_ms and seg.end_time_ms() > current_time_ms
        ]
        
        logger.info(
            f"Found {len(relevant_segments)} segments for time range "
            f"{current_time_ms}ms - {end_time_ms}ms"
        )
        
        return relevant_segments


    def _dash_fetch_task(self, current_time_ms: int, look_ahead_ms: int, buffer: DashStreamBuffer):

        if self.manifest.programme_id is None:
            logger.error(
                "Programme ID is not set. Create DASHMediaFetcher with a valid "
                "programme ID."
            )
            logger.debug("Exiting _dash_fetch_task due to missing programme ID.")
            buffer.close()
            return

       
        relevant_segments = self.get_segments_for_range(current_time_ms, look_ahead_ms)

        if len(relevant_segments) > 0:
            init_segment_url = relevant_segments[0].init_segment_url
            try:
                logger.debug(f"Downloading init segment: {init_segment_url}")
                response = requests.get(init_segment_url, timeout=10)
                response.raise_for_status()
                buffer.write(response.content)
            except requests.RequestException as e:
                logger.error(f"Failed to download init segment: {e}")

        logger.info(f"Downloading {len(relevant_segments)} relevant segments to buffer.")

  
        segment_count = len(relevant_segments)
        segment_index = 0

        while segment_index < segment_count and not self._stop_event.is_set():
            segment = relevant_segments[segment_index]

            try:
                logger.info(f"Downloading segment {segment.index}: {segment.url}")
                logger.info(f"Segment {segment.index}: start time: {segment.start_time_ms}ms, duration: {segment.duration_ms}ms ")

                response = requests.get(segment.url, timeout=10)
                response.raise_for_status()
                if buffer.write(response.content):
                        logger.info(f"Segment {segment.index + 1} written to buffer successfully.")
                        segment_index += 1
                else:
                    logger.warning(f"Buffer write failed for segment {segment.index}. Retrying...")
                    sleep(1)  # Wait a bit before retrying
                    while not buffer.write(response.content) and not self._stop_event.is_set():
                        logger.warning(f"Buffer write failed for segment {segment.index}. Retrying...")
                        sleep(1)  # Wait a bit before retrying
                    logger.info(f"Segment {segment.index} written to buffer successfully.")
                    segment_index += 1  # Move to the next segment after successful write
            except requests.RequestException as e:
                logger.error(f"Failed to download segment {segment.index}: {e}")
     
        buffer.write(None) # Signal EOF to the buffer
        logger.info(f"Downloaded {segment_index - 1} of {segment_count} segments for time range "
                    f"{current_time_ms}ms - {current_time_ms + look_ahead_ms}ms")
        logger.info("Downloader thread exiting.")

     


    def fetch_segments_to_buffer(
        self,
        current_time_ms: int,
        look_ahead_ms: int,
        buffer: DashStreamBuffer
    ) -> threading.Thread:
        """Start fetcher thread and return it."""
        self.thread = threading.Thread(target=self._dash_fetch_task, args=(current_time_ms, look_ahead_ms, buffer))
        self.thread.start()
        return self.thread
    
    def stop_fetching(self):
        """Signal the fetcher thread to stop gracefully."""
        logger.info("Signaling fetcher thread to stop.")
        self._stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                logger.warning("Fetcher thread did not exit within timeout")
            else:
                logger.info("Fetcher thread exited successfully.")
    
    def get_selected_fps(self) -> Optional[int]:
        """Return the FPS of the selected representation, or None if unknown."""
        if self.selected_representation is None:
            return None
        return self.selected_representation.frame_rate

    def get_selected_representation(self):
        """Return the selected representation."""
        return self.selected_representation


