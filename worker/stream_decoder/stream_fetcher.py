import logging
import sys
from datetime import datetime, timezone
from fractions import Fraction

import av
from .circular_buffer import CircularBuffer
from .media_decoder import DASHAudioDecoder, DASHStreamDecoder, DecodedFrame, FramePool
from .media_fetcher import DASHMediaFetcher, DASHRepresentation, DashStreamBuffer

MAX_SEGMENTS = 5
BUFFER_CAPACITY = 100  # 100 DecodedFrame object references
POOL_SIZE = 200  # Number of decoded frames to keep in pool
AUDIO_BUFFER_CAPACITY = 400  # DecodedAudioFrame object references
AUDIO_POOL_SIZE = 400  # Number of decoded chunks to keep in pool
AUDIO_CHANNELS = 2
AUDIO_SAMPLES_PER_CHUNK = 1024
AUDIO_BITRATE = 128000

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def fetch_dash_stream_audio(
    programme_id: str,
    start_time_ms: int,
    look_ahead_ms: int,
    output_file: str,
    output_sr: int = 48_000,
):
    # Fresh buffers per call: the fetcher closes its buffer at EOF, so module-level
    # buffers would be unusable for the next job in a long-lived worker.
    audio_buffer = DashStreamBuffer(max_size=MAX_SEGMENTS)  # buffer for audio DASH segments
    audio_ring_buffer = CircularBuffer(capacity=AUDIO_BUFFER_CAPACITY)  # decoded audio chunks

    audio_downloader = DASHMediaFetcher(programme_id)

    def on_audio_representation_available(representation: DASHRepresentation):
        logger.info(f"Audio representation selected: {representation}")

    if not audio_downloader.init(
        media_type="audio",
        quality={"bitrate": AUDIO_BITRATE},
        rep_selected_func=on_audio_representation_available,
    ):
        raise RuntimeError(f"Could not load DASH audio for programme {programme_id}")

    audio_decoder = DASHAudioDecoder(
        audio_buffer,
        audio_ring_buffer,
        pool_size=AUDIO_POOL_SIZE,
        channels=AUDIO_CHANNELS,
        num_samples=AUDIO_SAMPLES_PER_CHUNK,
    )
    audio_decoder.start_decoding()
    audio_downloader.fetch_segments_to_buffer(
        current_time_ms=start_time_ms,
        look_ahead_ms=look_ahead_ms,
        buffer=audio_buffer,
    )

    layout = "stereo"
    stream_type = "pcm_s16le"
    next_input_pts = 0
    output_format = "wav"

    with av.open(output_file, mode="w", format=output_format) as output:
        stream = output.add_stream(stream_type, rate=output_sr)
        stream.layout = layout

        try:
            while True:
                # Check liveness before popping: if the decoder had already exited
                # and the pop still comes back empty, nothing more can arrive.
                decoding = audio_decoder.thread.is_alive()
                chunk = audio_ring_buffer.pop(timeout=0.1)
                if chunk is None:
                    if not decoding:
                        break
                    continue

                try:
                    frame = av.AudioFrame.from_ndarray(
                        chunk.samples,
                        format=chunk.sample_format,
                        layout=layout,
                    )
                    frame.sample_rate = chunk.sample_rate
                    frame.pts = next_input_pts
                    frame.time_base = Fraction(1, chunk.sample_rate)
                    next_input_pts += frame.samples

                    for packet in stream.encode(frame):
                        output.mux(packet)
                finally:
                    audio_decoder.release_chunk(chunk)
        finally:
            audio_downloader.stop_fetching()
            audio_decoder.stop_decoding()

            # signal the end of the stream
            for packet in stream.encode(None):
                output.mux(packet)

    # The decoder logs and swallows its own errors, so an empty output is the
    # only sign that nothing could be decoded.
    if next_input_pts == 0:
        raise RuntimeError(f"No audio decoded from DASH stream for programme {programme_id}")
