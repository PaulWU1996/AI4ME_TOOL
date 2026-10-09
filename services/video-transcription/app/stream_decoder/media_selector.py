"""
BBC MediaSelector: Fetch streaming URLs for programmes using their IDs.
"""

import urllib.request
import json
from typing import Optional, Dict, List, Any
from dataclasses import dataclass
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("MediaSelector")

# Constants
CACHE_TTL_MS: int = 300000  # 5 minutes in milliseconds
FORMAT_DASH = 1
FORMAT_HLS = 2


@dataclass
class Connection:
    """Represents a media connection with streaming URL and format."""
    href: str
    transfer_format: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Connection':
        """Create Connection from dictionary (JSON response)."""
        return cls(
            href=data.get('href', ''),
            transfer_format=data.get('transferFormat')
        )


@dataclass
class Media:
    """Represents media with connection information."""
    connection: Optional[List[Connection]] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Media':
        """Create Media from dictionary (JSON response)."""
        connections = None
        if data.get('connection'):
            connections = [Connection.from_dict(c) for c in data['connection']]
        return cls(connection=connections)


@dataclass
class MediaSelectorResponse:
    """Response from BBC MediaSelector API."""
    media: Optional[List[Media]] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'MediaSelectorResponse':
        """Create MediaSelectorResponse from dictionary (JSON response)."""
        media = None
        if data.get('media'):
            media = [Media.from_dict(m) for m in data['media']]
        return cls(media=media)

    def get_stream_url(self) -> Optional[str]:
        """Extract the appropriate stream URL based on format."""
        format_name = stream_format()
        
        if not self.media:
            return None
        
        for media in self.media:
            if media.connection:
                for connection in media.connection:
                    if connection.transfer_format == format_name:
                        return connection.href
        
        return None


def supported_formats() -> int:
    """
    Determine supported formats for this platform.
    Currently returns both DASH and HLS as supported.
    """
    return FORMAT_DASH | FORMAT_HLS


def mediaset() -> str:
    """
    Determine the appropriate mediaset based on platform capabilities.
    
    Returns:
        'iptv-native-hd' for HLS-only platforms (Apple)
        'iptv-mse' for DASH-capable platforms
    """
    formats = supported_formats()
    
    # HLS-only platforms (Apple) use native player
    if (formats & FORMAT_HLS) != 0 and (formats & FORMAT_DASH) == 0:
        return "iptv-native-hd"
    
    # DASH-capable platforms use MSE
    return "iptv-mse"


def stream_format() -> str:
    """
    Determine the appropriate stream format based on platform capabilities.
    
    Returns:
        'dash' if DASH is supported
        'hls' if only HLS is supported
        'dash' as default fallback
    """
    formats = supported_formats()
    
    if (formats & FORMAT_DASH) != 0:
        return "dash"
    
    if (formats & FORMAT_HLS) != 0:
        return "hls"
    
    # Default to DASH
    return "dash"


def build_url(pid: str) -> str:
    """
    Build BBC MediaSelector API URL for a given programme ID.
    
    Args:
        pid: BBC programme ID (e.g., 'b0bccccc')
    
    Returns:
        Full URL to BBC MediaSelector API
    """
    return (
        f"https://open.live.bbc.co.uk/mediaselector/6/select/version/3.0/"
        f"mediaset/{mediaset()}/cvid/urn:bbc:pips:pid:{pid}/"
        f"format/json/proto/https/cors/1"
    )


def request_programme(programme_id: str) -> Optional[str]:
    """
    Fetch streaming URL for a programme using its ID.
    
    Args:
        programme_id: BBC programme ID (VPID)
    
    Returns:
        Stream URL if available, None otherwise
    """
    url = build_url(programme_id)
    logger.info(f"Requesting programme ID: {programme_id}")
    
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
            media_response = MediaSelectorResponse.from_dict(data)

            # print(f"Media response: {media_response}")
            
            stream_url = media_response.get_stream_url()
            if stream_url:
                logger.info(
                    f"Found {stream_format().upper()} stream for {programme_id}"
                )
                return stream_url
            else:
                logger.warning(
                    f"No {stream_format().upper()} URL found for {programme_id}"
                )
                return None
    
    except urllib.error.URLError as e:
        logger.error(f"Request failed for {programme_id}: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error for {programme_id}: {e}")
        return None


def request_live(service_id: str) -> Optional[str]:
    """
    Fetch streaming URL for a live service using its ID.
    Includes caching support (TTL in milliseconds).
    
    Args:
        service_id: BBC service ID (e.g., 'bbc_one')
    
    Returns:
        Stream URL if available, None otherwise
    """
    url = build_url(service_id)
    logger.info(f"Requesting live service: {service_id}")
    
    try:
        # Note: Caching (CACHE_TTL_MS) would typically be handled with requests-cache
        # or similar library. For this basic implementation, we make a fresh request.
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
            media_response = MediaSelectorResponse.from_dict(data)
            
            stream_url = media_response.get_stream_url()
            if stream_url:
                logger.info(
                    f"Found {stream_format().upper()} stream for live {service_id}"
                )
                return stream_url
            else:
                logger.warning(
                    f"No {stream_format().upper()} URL found for live {service_id}"
                )
                return None
    
    except urllib.error.URLError as e:
        logger.error(f"Live request failed for {service_id}: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error for live {service_id}: {e}")
        return None


def main():
    """Test the MediaSelector functions."""
    print("=" * 70)
    print("BBC MediaSelector - Python Implementation")
    print("=" * 70)
    
    # Test 1: Check platform capabilities
    print("\n[Test 1] Platform Capabilities")
    print(f"Supported formats: DASH={bool(supported_formats() & FORMAT_DASH)}, "
          f"HLS={bool(supported_formats() & FORMAT_HLS)}")
    print(f"Mediaset: {mediaset()}")
    print(f"Stream format: {stream_format()}")
    
    # Test 2: URL building
    print("\n[Test 2] URL Building")
    test_pid = "m002vqlg"
    url = build_url(test_pid)
    print(f"Programme ID: {test_pid}")
    print(f"Built URL: {url}")
    
    # Test 3: Fetch programme stream (using a real BBC ID)
    print("\n[Test 3] Fetch Programme Stream")
    print("Attempting to fetch stream for a BBC programme...")
    # Using a test ID - note: this may or may not work depending on BBC availability
    result = request_programme("m002vqlg")
    if result:
        print(f"Stream URL obtained: {result}")
    else:
        print("✗ No stream URL found (programme may be unavailable)")
    
    # # Test 4: Fetch live stream
    # print("\n[Test 4] Fetch Live Stream")
    # print("Attempting to fetch live stream...")
    # result = request_live("bbc_one")
    # if result:
    #     print(f"✓ Live stream URL obtained: {result[:80]}...")
    # else:
    #     print("✗ No live stream URL found (service may be unavailable)")
    
    # print("\n" + "=" * 70)
    # print("Tests completed")
    # print("=" * 70)


if __name__ == "__main__":
    main()
