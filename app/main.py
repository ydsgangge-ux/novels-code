from fastapi import FastAPI
from app.routers import items

app = FastAPI(title="FastAPI Demo API", version="1.0.0")

app.include_router(items.router, prefix="/api/v1")


@app.get("/")
async def root():
    return {"message": "FastAPI Demo is running", "version": "1.0.0"}
