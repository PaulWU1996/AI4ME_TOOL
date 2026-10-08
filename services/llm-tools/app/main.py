from fastapi import FastAPI

from app.routers import process

app = FastAPI(title="AI4ME Transcript Processor", version="0.1.0")
app.include_router(process.router)