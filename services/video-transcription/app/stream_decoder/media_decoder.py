"""
Media Decoder: Decode MPEG-DASH H.264 segments (ISOBMFF) to RGB frames.

Uses FFmpeg's libav library via PyAV to decode H.264 video streams
and write decoded frames to an in-memory buffer.
"""

import logging

import threading
from time import time
from typing import Optional, Tuple, List

import numpy as np

import av


from .circular_buffer import CircularBuffer
from .media_fetcher import DashStreamBuffer

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("MediaDecoder")


class DecodedFrame:
    """
    Represents a single decoded video frame.
    """
    
    def __init__(
        self,
        rgb_data: np.ndarray,
        width: int,
        height: int,
        timestamp: float,
        frame_number: int
    ):
        """
        Initialize a decoded frame.
        
        Args:
            rgb_data: RGB pixel data as numpy array (H x W x 3)
            width: Frame width in pixels
            height: Frame height in pixels
            timestamp: Presentation timestamp in seconds
            frame_number: Sequence number of the frame
        """
        self.rgb_data = rgb_data
        self.width = width
        self.height = height
        self.timestamp = timestamp
        self.frame_number = frame_number

    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height
        self.rgb_data = np.zeros((height, width, 3), dtype=np.uint8)
        self.timestamp = 0.0
        self.frame_number = -1
    
    def size_bytes(self) -> int:
        """Get the size of this frame in bytes."""
        return self.rgb_data.nbytes
    
    def get_pixel(self, x: int, y: int) -> Tuple[int, int, int]:
        """
        Get RGB value at specific pixel coordinates.
        
        Args:
            x: X coordinate (0-width)
            y: Y coordinate (0-height)
            
        Returns:
            Tuple of (R, G, B) values
        """
        if 0 <= y < self.height and 0 <= x < self.width:
            rgb = self.rgb_data[y, x]
            return tuple(rgb.astype(int))
        return (0, 0, 0)


class FramePool:
    """
    Manages a pool of decoded frames for reuse.
    
    This class allows for efficient memory management by reusing
    DecodedFrame objects instead of creating new ones for each frame.
    """
    
    def __init__(self, capacity: int = 100, width: int = 1920, height: int = 1080):
        self.capacity = capacity
        self.pool = [DecodedFrame(width, height) for _ in range(capacity)]
        self.free_frames = self.pool.copy()
        self.lock = threading.Lock()
        self.available = threading.Semaphore(capacity) 
        
    
    def acquire_frame(
        self,
        rgb_data: np.ndarray,
        width: int,
        height: int,
        timestamp: float,
        frame_number: int
    ) -> DecodedFrame:
        """
        Acquire a frame from the pool or create a new one.
        
        Args:
            rgb_data: RGB pixel data as numpy array
            width: Frame width
            height: Frame height
            timestamp: Presentation timestamp
            frame_number: Sequence number
            
        Returns:
            DecodedFrame object
        """
        self.available.acquire()
        with self.lock:
            if self.free_frames:
                frame = self.free_frames.pop()
                # Copy data into pre-allocated frame buffer to avoid aliasing issues
                np.copyto(frame.rgb_data, rgb_data)
                frame.width = width
                frame.height = height
                frame.timestamp = timestamp
                frame.frame_number = frame_number
                return frame
            else:
                return DecodedFrame(rgb_data, width, height, timestamp, frame_number)

    def acquire_empty_frame(self, width: int, height: int) -> DecodedFrame:
        """
        Acquire an empty frame from the pool or block until one is available.
        
        Args:
            width: Frame width
            height: Frame height
            
        Returns:
            DecodedFrame object
        """
        self.available.acquire()
        with self.lock:
            if self.free_frames:
                frame = self.free_frames.pop()
                frame.width = width
                frame.height = height
                frame.timestamp = 0.0
                frame.frame_number = -1
                return frame
            else:
                # This should not happen as acquire blocks until a frame is available
                raise RuntimeError("Failed to acquire empty frame from the pool.")
    
    def release_frame(self, frame: DecodedFrame):
        """Release a frame back to the pool."""
        with self.lock:
            if len(self.free_frames) < self.capacity:
                self.free_frames.append(frame)
                self.available.release()

    def get_free_frame_count(self) -> int:
        """Return the number of free frames currently in the pool."""
        with self.lock:
            return len(self.free_frames)

    def clear(self):
        """Clear all frames from the pool."""
        with self.lock:
            self.free_frames.clear()
            # Reset the semaphore to reflect the cleared pool
            while self.available._value < self.capacity:
                self.available.release()



class MP4FileBuffer:
    def __init__(self, filepath):
        self.filepath = filepath
        # Open the file in raw binary read mode
        self.file = open(filepath, 'rb')
        
    def read(self, size: int) -> bytes:
        """Called internally by PyAV to pull the next chunk of bytes."""
        return self.file.read(size)
        
    def seek(self, offset: int, whence: int = 0) -> int:
        """
        Crucial for MP4 files! PyAV will call this to jump to the end 
        of the file to find the 'moov' atom, then jump back.
        """
        return self.file.seek(offset, whence)
        
    def tell(self) -> int:
        """PyAV calls this to know its current byte position."""
        return self.file.tell()
        
    def close(self):
        self.file.close()

class MP4FileDecoder:
    def __init__(self, file_path: str, frame_pool: FramePool, ring_buffer: CircularBuffer):
        self.file_path = file_path
        self.file_buffer = MP4FileBuffer(file_path)
        self.frame_pool = frame_pool
        self.ring_buffer = ring_buffer
        self.thread = None

    def start_decoding(self):
        """Start the decoding thread."""
        self.thread = threading.Thread(target=self.file_decode_thread, daemon=True)
        self.thread.start()

    def join(self):
        """Wait for the decoding thread to finish."""
        if self.thread:
            self.thread.join()

    def stop_decoding(self):
        """Stop the decoding thread and close the file buffer."""
        if self.thread and self.thread.is_alive():
            logger.info("Stopping decoding thread...")
            self.thread.join()
        self.file_buffer.close()

    def file_decode_thread(self):
        """
        Thread function to decode MP4 file and push frames into the ring buffer.
        """
        try:
            # Open the MP4 file using PyAV with our custom buffer
            container_obj = av.open(self.file_buffer, format='mp4')
            
            # Find the video stream
            video_stream = None
            for stream in container_obj.streams:
                if stream.type == 'video':
                    video_stream = stream
                    logger.info(
                        f"Found video stream: {stream.width}x{stream.height}, "
                        f"codec={stream.codec_context.name}"
                    )
                    break
            
            if not video_stream:
                raise ValueError("No video stream found in MP4 file")
            
            frame_num = 0
            for frame in container_obj.decode(video_stream):

                pooled_frame = self.frame_pool.acquire_empty_frame(frame.width, frame.height)

                pooled_frame.timestamp = float(frame.pts * video_stream.time_base) if frame.pts else 0.0
                pooled_frame.frame_number = frame_num
                pooled_frame.width = frame.width
                pooled_frame.height = frame.height
                np.copyto(pooled_frame.rgb_data, frame.to_rgb().to_ndarray())

                
                # Push the decoded frame into the ring buffer
                self.ring_buffer.push(pooled_frame)
                
                frame_num += 1
            
            container_obj.close()
            logger.info(f"Finished decoding {frame_num} frames from {self.file_path}")
        
        except Exception as e:
            logger.error(f"Error decoding MP4 file: {e}")
        
        finally:
            self.file_buffer.close()
            logger.info("MP4 file buffer closed and decoding thread exiting.")


class DecodedAudioFrame:
    """
    Represents a single decoded audio chunk.

    Audio arrives as interleaved or planar PCM samples, which are stored in a numpy array.
    """

    def __init__(
        self,
        samples: np.ndarray,
        sample_rate: int,
        channels: int,
        timestamp: float,
        frame_number: int,
        sample_format: str = "fltp",
    ):
        """
        Initialize a decoded audio chunk.

        Args:
            samples: PCM samples as numpy array (channels x num_samples)
            sample_rate: Sample rate in Hz
            channels: Number of audio channels
            timestamp: Presentation timestamp in seconds
            frame_number: Sequence number of the chunk
            sample_format: PyAV sample format name (e.g. 'fltp' for planar float32)
        """
        self.samples = samples
        self.sample_rate = sample_rate
        self.channels = channels
        self.timestamp = timestamp
        self.frame_number = frame_number
        self.sample_format = sample_format

    def size_bytes(self) -> int:
        """Get the size of this chunk's sample data in bytes."""
        return self.samples.nbytes

    def duration(self) -> float:
        """Get the duration of this chunk in seconds."""
        if self.sample_rate == 0:
            return 0.0
        samples_secs = self.samples.shape[-1] / self.sample_rate
        if self.is_planar():
            return samples_secs
        else:
            return samples_secs / self.channels

    def is_planar(self) -> bool:
        """Check whether samples are stored one channel per row."""
        return self.samples.ndim == 2 and self.samples.shape[0] == self.channels


class AudioChunkPool:
    """
    Manages a pool of decoded audio chunks for reuse.

    Mirrors FramePool, but sizes buffers by channel count and sample count rather than
    by pixels.
    """

    def __init__(
        self,
        capacity: int = 300,
        channels: int = 2,
        num_samples: int = 1024,
    ):
        """
        Args:
            capacity: Maximum number of chunks to retain for reuse
            channels: Expected channel count for pre-allocated chunks
            num_samples: Expected samples per channel for pre-allocated chunks
        """
        self.capacity = capacity
        self.channels = channels
        self.num_samples = num_samples
        self.pool = [
            DecodedAudioFrame(
                samples=np.zeros((channels, num_samples), dtype=np.float32),
                sample_rate=48000,
                channels=channels,
                timestamp=0.0,
                frame_number=-1,
            )
            for _ in range(capacity)
        ]
        self.free_frames = self.pool.copy()
        self.lock = threading.Lock()
        self.available = threading.Semaphore(capacity)

    def acquire(
        self,
        samples: np.ndarray,
        sample_rate: int,
        channels: int,
        timestamp: float,
        frame_number: int,
        sample_format: str = "fltp",
    ) -> DecodedAudioFrame:
        """
        Acquire a chunk from the pool and copy samples into it.

        Blocks until a chunk is free. If the incoming shape does not match the
        pre-allocated one, the pre-allocated buffer is replaced rather than raising.
        AAC frame sizes vary, so a shape that differs from the configured size is an
        expected runtime condition rather than a misconfiguration.

        Args:
            samples: PCM samples as numpy array
            sample_rate: Sample rate in Hz
            channels: Number of channels
            timestamp: Presentation timestamp in seconds
            frame_number: Sequence number
            sample_format: PyAV sample format name

        Returns:
            DecodedAudioFrame object
        """
        self.available.acquire()
        with self.lock:
            if not self.free_frames:
                return DecodedAudioFrame(
                    samples, sample_rate, channels, timestamp, frame_number, sample_format
                )

            chunk = self.free_frames.pop()
            if chunk.samples.shape == samples.shape:
                np.copyto(chunk.samples, samples)
            else:
                # Shape changed, so the pre-allocated buffer no longer fits
                chunk.samples = np.array(samples, copy=True)

            chunk.sample_rate = sample_rate
            chunk.channels = channels
            chunk.timestamp = timestamp
            chunk.frame_number = frame_number
            chunk.sample_format = sample_format
            return chunk

    def release_frame(self, frame: DecodedAudioFrame):
        """Release a chunk back to the pool."""
        with self.lock:
            if len(self.free_frames) < self.capacity:
                self.free_frames.append(frame)
                self.available.release()

    def get_free_frame_count(self) -> int:
        """Return the number of free chunks currently in the pool."""
        with self.lock:
            return len(self.free_frames)

    def clear(self):
        """Clear all chunks from the pool."""
        with self.lock:
            self.free_frames.clear()
            # Reset the semaphore to reflect the cleared pool
            while self.available._value < self.capacity:
                self.available.release()


class DASHAudioDecoder:
    """
    Decodes an MPEG-DASH audio stream to raw PCM chunks.

    Mirrors DASHStreamDecoder: it owns its own AudioChunkPool and exposes
    release_chunk() for consumers returning chunks.
    """

    def __init__(
        self,
        stream_buffer: DashStreamBuffer,
        decoded_buffer: CircularBuffer,
        pool_size: int = 300,
        channels: int = 2,
        num_samples: int = 1024,
        pts_offset: float = 0.0,
    ):
        """
        Args:
            stream_buffer: Buffer of audio init and media segments
            decoded_buffer: Buffer that decoded chunks are pushed into
            pool_size: Maximum number of chunks to retain for reuse
            channels: Expected channel count for pre-allocated chunks
            num_samples: Expected samples per channel for pre-allocated chunks
            pts_offset: Seconds added to each chunk's presentation timestamp. The
                video and audio representations use different time bases, so their
                first frames do not share a timestamp. Defaults to 0, leaving
                alignment to the consumer.
        """
        self.stream_buffer = stream_buffer
        self.chunk_pool = AudioChunkPool(
            capacity=pool_size, channels=channels, num_samples=num_samples
        )
        self.ring_buffer = decoded_buffer
        self.pts_offset = pts_offset
        self.thread = None
        self._stop_event = threading.Event()

    def start_decoding(self):
        """Start the decoding thread."""
        if self.thread is None:
            self.thread = threading.Thread(target=self._decode_func, daemon=True)
            self.thread.start()

    def _decode_func(self):
        logger.info("Starting audio decoding thread. Waiting for initial segment ...")
        try:
            container = av.open(self.stream_buffer, format='mp4')
            logger.info("Audio DASH container opened successfully.")

            audio_stream = None
            for stream in container.streams:
                if stream.type == 'audio':
                    audio_stream = stream
                    logger.info(
                        f"Found audio stream: codec={stream.codec_context.name}, "
                        f"sample_rate={stream.codec_context.sample_rate}, "
                        f"time_base={stream.time_base}"
                    )
                    break

            if not audio_stream:
                raise ValueError("No audio stream found in DASH stream")

            chunk_num = 0
            for frame in container.decode(audio_stream):
                if self._stop_event.is_set():
                    logger.info("Stop signal received. Exiting audio decode loop.")
                    break

                # frame.layout.channels is a tuple of channel names, not a count
                channels = len(frame.layout.channels)
                timestamp = (
                    float(frame.pts * audio_stream.time_base) if frame.pts else 0.0
                ) + self.pts_offset

                chunk = self.chunk_pool.acquire(
                    samples=frame.to_ndarray(),
                    sample_rate=frame.sample_rate,
                    channels=channels,
                    timestamp=timestamp,
                    frame_number=chunk_num,
                    sample_format=frame.format.name,
                )
                self.ring_buffer.push(chunk)
                chunk_num += 1

            container.close()
            logger.info(f"Finished decoding {chunk_num} audio chunks")

        except Exception as e:
            logger.error(f"Unexpected error decoding DASH audio stream: {e}")
        finally:
            logger.info("Audio decoding thread exiting.")

    def stop_decoding(self):
        """Signal the audio decoder thread to stop gracefully."""
        logger.info("Signaling audio decoder thread to stop.")
        self.stream_buffer.write(None)
        self._stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)  # Wait max 5 seconds
            if self.thread.is_alive():
                logger.warning("Audio decoder thread did not exit within timeout")

    def release_chunk(self, chunk: DecodedAudioFrame):
        """Release a chunk back to the pool."""
        self.chunk_pool.release_frame(chunk)

    def cleanup(self):
        """Clear the chunk pool."""
        self.chunk_pool.clear()

    def get_free_chunk_count(self):
        """Get the number of free chunks available in the pool."""
        return self.chunk_pool.get_free_frame_count()


class DASHStreamDecoder:
    def __init__(self, stream_buffer: DashStreamBuffer, decoded_buffer: CircularBuffer, pool_size: int = 5000, frame_width: int = 960, frame_height: int = 540):
        self.stream_buffer = stream_buffer
        self.frame_pool = FramePool(capacity=pool_size, width=frame_width, height=frame_height)
        self.ring_buffer = decoded_buffer
        self.thread = None
        self._stop_event = threading.Event()

    def start_decoding(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._decode_func)
            self.thread.start()

    def _decode_func(self):
        logger.info("Starting decoding thread. Waiting for initial segment ...")
        try:
            container = av.open(self.stream_buffer, format='mp4')
            logger.info("DASH stream container opened successfully.")
            logger.info("Starting to decode video frames from DASH stream.")

            # Find the video stream
            video_stream = None
            for stream in container.streams:
                if stream.type == 'video':
                    video_stream = stream
                    logger.info(
                        f"Found video stream: {stream.width}x{stream.height}, "
                        f"codec={stream.codec_context.name}, "
                        f"time_base={stream.time_base}"
                    )
                    break
            
            if not video_stream:
                raise ValueError("No video stream found in DASH stream")
            
            frame_num = 0

            for frame in container.decode(video_stream):
                # Check if stop signal was received
                if self._stop_event.is_set():
                    logger.info("Stop signal received. Exiting decode loop.")
                    break
                # Get an empty frame from pool or block until one becomes available
                pooled_frame = self.frame_pool.acquire_empty_frame(frame.width, frame.height)

                # logger.info(f"Decoded frame {frame_num}: width={frame.width}, height={frame.height}, pts={frame.pts}, timestamp={float(frame.pts * video_stream.time_base) if frame.pts else 0.0}")
                
                pooled_frame.timestamp = float(frame.pts * video_stream.time_base) if frame.pts else 0.0
                pooled_frame.frame_number = frame_num
                pooled_frame.width = frame.width
                pooled_frame.height = frame.height
                np.copyto(pooled_frame.rgb_data, frame.to_rgb().to_ndarray())
                self.ring_buffer.push(pooled_frame)
                frame_num += 1

            container.close()
            logger.info(f"Finished decoding {frame_num} frames from DASH stream.")

        except Exception as e:
            logger.error(f"Error decoding DASH stream: {e}")
        finally:
            logger.info("Decoding thread exiting.")
            

    def stop_decoding(self):
        """Signal the decoder thread to stop gracefully."""
        logger.info("Signaling decoder thread to stop.")
        self.stream_buffer.write(None)
        self._stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)  # Wait max 5 seconds
            if self.thread.is_alive():
                logger.warning("Decoder thread did not exit within timeout")


    def release_frame(self, frame):
        """Release a frame back to the pool."""
        self.frame_pool.release_frame(frame)


    def cleanup(self):
        """Clear the frame pool."""
        self.frame_pool.clear()

    def get_free_frame_count(self):
        """Get the number of free frames available in the pool."""
        return self.frame_pool.get_free_frame_count()
