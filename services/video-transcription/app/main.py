import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.routers import process
from app.services import gemma_runner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load the weights before the port opens, so a job never waits on a cold
    # start and a broken model/GPU fails the container instead of every request.
    gemma_runner.warm_up()
    yield


app = FastAPI(title="AI4ME Video Transcription", version="0.1.0", lifespan=lifespan)
app.include_router(process.router)
