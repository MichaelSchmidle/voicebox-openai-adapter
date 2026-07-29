from voicebox_openai_adapter.app import create_app
from voicebox_openai_adapter.config import Settings
from voicebox_openai_adapter.logging_config import configure_logging

settings = Settings()
configure_logging(settings.log_level)
app = create_app(settings=settings)
