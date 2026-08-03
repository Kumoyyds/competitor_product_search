"""Offline verification for M18 -- provider-aware LLM client construction."""

from __future__ import annotations

import logging
import os
import tempfile
import traceback
from pathlib import Path
from unittest.mock import patch

from src.scraping.config import ScrapingConfig, set_config
from src.scraping.providers import (
    PROVIDERS,
    make_chat_client,
    resolve_provider,
)

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  [PASS] {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  ({detail})")


def section(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


class FakeChatOpenAI:
    calls: list[dict] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.__class__.calls.append(kwargs)


def verify_resolution() -> None:
    section("M18.1 - provider resolution")
    model, spec = resolve_provider("deepseek-v4-pro")
    check(
        "registered DeepSeek model resolves",
        model == "deepseek-v4-pro" and spec is PROVIDERS["deepseek"],
    )

    model, spec = resolve_provider("deepseek/private-preview")
    check(
        "explicit provider prefix wins",
        model == "private-preview" and spec is PROVIDERS["deepseek"],
    )

    model, spec = resolve_provider("qwen3.7-plus")
    check(
        "registered Qwen model resolves",
        model == "qwen3.7-plus" and spec is PROVIDERS["qwen"],
    )

    with _capture_provider_warnings() as records:
        model, spec = resolve_provider("configured-coldstart-model")
    check(
        "unknown model falls back without raising",
        model == "configured-coldstart-model" and spec is PROVIDERS["qwen"],
    )
    check(
        "unknown-model fallback emits warning",
        any("falling back" in record.getMessage() for record in records),
    )


def verify_key_resolution() -> None:
    section("M18.2 - dynamic provider key resolution")
    with tempfile.TemporaryDirectory() as temp_dir:
        env_path = Path(temp_dir) / ".env"
        env_path.write_text(
            "DEEPSEEK_KEY=dotenv-deepseek-key\n"
            "SCRAPING_FAKE_VENDOR_KEY=dotenv-prefixed-key\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {}, clear=False):
            for name in (
                "DEEPSEEK_KEY",
                "SCRAPING_DEEPSEEK_KEY",
                "FAKE_VENDOR_KEY",
                "SCRAPING_FAKE_VENDOR_KEY",
            ):
                os.environ.pop(name, None)
            cfg = ScrapingConfig(_env_file=env_path)
            check(
                "key loads from configured dotenv only",
                cfg.api_key_for("DEEPSEEK_KEY") == "dotenv-deepseek-key",
            )
            check(
                "SCRAPING_ dotenv alias is honored",
                cfg.api_key_for("FAKE_VENDOR_KEY") == "dotenv-prefixed-key",
            )

            os.environ["SCRAPING_DEEPSEEK_KEY"] = "environment-wins"
            check(
                "os.environ has precedence over dotenv",
                cfg.api_key_for("DEEPSEEK_KEY") == "environment-wins",
            )
            os.environ.pop("SCRAPING_DEEPSEEK_KEY")
            os.environ["DEEPSEEK_KEY"] = ""
            check(
                "explicit empty environment key masks dotenv",
                cfg.api_key_for("DEEPSEEK_KEY") == "",
            )

    cfg = ScrapingConfig(qwen_key="programmatic-qwen-key", _env_file=None)
    check(
        "programmatic legacy key remains compatible",
        cfg.api_key_for("QWEN_KEY") == "programmatic-qwen-key",
    )


def verify_client_factory() -> None:
    section("M18.3 - unified client factory")
    cfg = ScrapingConfig(
        qwen_base_url="https://qwen-compatible.example/v1", _env_file=None
    )
    set_config(cfg)
    FakeChatOpenAI.calls.clear()

    with (
        patch("langchain_openai.ChatOpenAI", new=FakeChatOpenAI),
        patch.object(ScrapingConfig, "api_key_for", return_value="deepseek-test-key"),
    ):
        deepseek_plain = make_chat_client("deepseek-v4-flash", purpose="m18 plain")
        deepseek_thinking = make_chat_client(
            "deepseek/deepseek-v4-pro",
            temperature=0.4,
            enable_thinking=True,
            purpose="m18 thinking",
        )
        qwen_thinking = make_chat_client(
            "qwen3.7-plus", enable_thinking=True, purpose="m18 qwen"
        )

    plain_args, thinking_args, qwen_args = FakeChatOpenAI.calls
    check("DeepSeek client is constructed", deepseek_plain is not None)
    check(
        "DeepSeek official endpoint selected",
        plain_args.get("base_url") == "https://api.deepseek.com",
    )
    check(
        "DeepSeek key channel is independent",
        plain_args.get("api_key") == "deepseek-test-key",
    )
    check(
        "explicit prefix is stripped from model",
        thinking_args.get("model") == "deepseek-v4-pro",
    )
    check(
        "JSON object response format retained",
        plain_args.get("model_kwargs", {}).get("response_format")
        == {"type": "json_object"},
    )
    check(
        "DeepSeek ordinary node disables default thinking",
        plain_args.get("extra_body") == {"thinking": {"type": "disabled"}},
    )
    check(
        "DeepSeek last node enables thinking",
        thinking_args.get("extra_body") == {"thinking": {"type": "enabled"}},
    )
    check(
        "Qwen uses its own thinking parameter",
        qwen_args.get("extra_body") == {"enable_thinking": True},
    )
    check(
        "legacy Qwen endpoint override remains compatible",
        qwen_args.get("base_url") == "https://qwen-compatible.example/v1",
    )

    with (
        patch("langchain_openai.ChatOpenAI", new=FakeChatOpenAI),
        patch.object(ScrapingConfig, "api_key_for", return_value=""),
    ):
        missing = make_chat_client("deepseek-v4-flash", purpose="missing-key check")
    check("missing provider key degrades to None", missing is None)


def verify_call_sites() -> None:
    section("M18.4 - all call sites use configured models")
    from src.scraping.repair import agent, json_healer

    cfg = ScrapingConfig(
        qwen_key="offline-key",
        repair_model_ladder=["configured-first-model"],
        repair_temperature_ladder=[0.1],
        _env_file=None,
    )
    set_config(cfg)

    agent_args: dict = {}
    healer_args: dict = {}

    def fake_agent_client(**kwargs):
        agent_args.update(kwargs)
        return object()

    def fake_healer_client(**kwargs):
        healer_args.update(kwargs)
        return object()

    with patch.object(agent, "make_chat_client", side_effect=fake_agent_client):
        agent_client = agent._make_llm(
            "deepseek-v4-pro", temperature=0.4, enable_thinking=True
        )
    check("repair _make_llm remains a working thin wrapper", agent_client is not None)
    check(
        "repair wrapper forwards model/temperature/thinking",
        agent_args.get("model") == "deepseek-v4-pro"
        and agent_args.get("temperature") == 0.4
        and agent_args.get("enable_thinking") is True,
        str(agent_args),
    )

    with patch.object(json_healer, "make_chat_client", side_effect=fake_healer_client):
        healer_client = json_healer._make_llm()
    check("JSON healer constructs a client", healer_client is not None)
    check(
        "JSON healer uses ladder first model",
        healer_args.get("model") == "configured-first-model",
        str(healer_args.get("model")),
    )


class _capture_provider_warnings:
    def __enter__(self):
        self.records = []
        self.handler = _ListHandler(self.records)
        logging.getLogger("src.scraping.providers").addHandler(self.handler)
        return self.records

    def __exit__(self, exc_type, exc_value, tb):
        logging.getLogger("src.scraping.providers").removeHandler(self.handler)


class _ListHandler(logging.Handler):
    def __init__(self, records):
        super().__init__(logging.WARNING)
        self.records = records

    def emit(self, record):
        self.records.append(record)


def main() -> int:
    try:
        verify_resolution()
        verify_key_resolution()
        verify_client_factory()
        verify_call_sites()
    except Exception:
        FAILED.append(("EXCEPTION", traceback.format_exc()))
        traceback.print_exc()

    print()
    print("=" * 70)
    print(f"SUMMARY: {len(PASSED)} passed, {len(FAILED)} failed")
    print("=" * 70)
    if FAILED:
        for name, detail in FAILED:
            print(f"  FAILED: {name}: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
