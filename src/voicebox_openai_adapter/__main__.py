import uvicorn


def main() -> None:
    uvicorn.run(
        "voicebox_openai_adapter.main:app",
        host="0.0.0.0",
        port=8000,
        access_log=False,
    )


if __name__ == "__main__":
    main()
