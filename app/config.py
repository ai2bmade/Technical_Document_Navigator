from pathlib import Path
import os


def env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


class Settings:
    def __init__(self) -> None:
        self.app_name = env("APP_NAME", "Industrial Technical Document Copilot") or ""
        self.app_data_dir = Path(env("APP_DATA_DIR", "data") or "data")
        self.database_url = env("DATABASE_URL")
        self.chroma_host = env("CHROMA_HOST", "localhost") or "localhost"
        self.chroma_port = int(env("CHROMA_PORT", "8000") or "8000")
        self.telegram_bot_token = env("TELEGRAM_BOT_TOKEN")
        self.openai_api_key = env("OPENAI_API_KEY")
        self.openai_model = env("OPENAI_MODEL", "gpt-5.2") or "gpt-5.2"
        self.tesseract_cmd = env(
            "TESSERACT_CMD",
            r"G:\Codex\tools\tesseract\Tesseract-OCR\tesseract.exe",
        ) or "tesseract"
        self.ocr_lang = env("OCR_LANG", "kor+eng") or "kor+eng"
        self.ocr_dpi = int(env("OCR_DPI", "220") or "220")

    @property
    def uploads_dir(self) -> Path:
        return self.app_data_dir / "uploads"

    @property
    def logs_dir(self) -> Path:
        return self.app_data_dir / "logs"

    @property
    def vector_dir(self) -> Path:
        return self.app_data_dir / "vectorstore"

    @property
    def ocr_tmp_dir(self) -> Path:
        return self.app_data_dir / "ocr_tmp"

    @property
    def sqlite_path(self) -> Path:
        return self.app_data_dir / "data" / "industrial_copilot.sqlite3"


settings = Settings()
