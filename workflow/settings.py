from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """App settings, read from environment variables prefixed with `WORKFLOW_`."""

    model_config = SettingsConfigDict(env_prefix="WORKFLOW_")

    # Backstop against links that keep changing each other's state back and forth.
    max_cascade_steps: int = Field(default=100, gt=0)

    # Automatic tasks pretend to work for a random time between 0 and this many seconds.
    max_task_seconds: float = Field(default=10, ge=0)
    # How often the pretend work fails, from 0 (never) to 1 (always). Shows retries and failures.
    task_failure_rate: float = Field(default=0, ge=0, le=1)

    # How many automatic tasks run at once. More wait in a queue.
    task_workers: int = Field(default=4, gt=0)
    # Time limit for one attempt at an automatic task.
    task_timeout_seconds: float = Field(default=30, gt=0)
    # Attempts before an automatic task is marked failed.
    task_max_attempts: int = Field(default=3, gt=0)
    # Wait before the first retry. It doubles for each retry after that.
    task_retry_seconds: float = Field(default=1, ge=0)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
