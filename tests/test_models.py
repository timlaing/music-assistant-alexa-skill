"""Bundled voice-model contracts for issue #71 (invocation name + MA tails)."""

import json
from pathlib import Path

import verification

MODELS = Path(__file__).resolve().parent.parent / "app" / "models"
BUILT_IN = MODELS / "built-in"
INVOCATION = "music assistant"


def _locales():
    return sorted(path.stem for path in MODELS.glob("*.json"))


def _language(data):
    return data["interactionModel"]["languageModel"]


def _model(locale):
    return _language(
        json.loads((MODELS / f"{locale}.json").read_text(encoding="utf-8"))
    )


def _built_in(locale):
    return _language(
        json.loads((BUILT_IN / f"{locale}.json").read_text(encoding="utf-8"))
    )


def _play_audio_samples(locale):
    return next(
        intent["samples"]
        for intent in _model(locale)["intents"]
        if intent["name"] == "PlayAudio"
    )


def test_every_custom_model_invokes_music_assistant():
    # Music Assistant hardcodes `music assistant` in its start commands
    # (ALEXA_LANGUAGE_COMMANDS); the bundled models are the source of truth.
    locales = _locales()
    assert len(locales) > 10  # a glob mistake must not silently pass
    for locale in locales:
        assert _model(locale)["invocationName"] == INVOCATION, locale


def test_every_music_assistant_command_addresses_the_invocation():
    # The table is copied from Music Assistant; pin the locales it must cover
    # so a dropped entry fails here instead of shrinking this test.
    assert set(verification.MA_START_COMMANDS) == {
        "de-DE",
        "en-CA",
        "en-US",
        "es-ES",
        "fr-CA",
        "fr-FR",
        "it-IT",
        "nl-NL",
        "pt-BR",
    }
    commands = [
        verification.MA_DEFAULT_START_COMMAND,
        *verification.MA_START_COMMANDS.values(),
    ]
    for command in commands:
        assert verification._names_invocation(command, INVOCATION), command


def test_play_audio_samples_cover_the_music_assistant_start_command_tail():
    # Where MA's start command is the built-in prefix + a tail ("ask music
    # assistant to play audio"), PlayAudio must offer that exact tail or the
    # composed utterance never matches an intent.
    checked = 0
    for locale in _locales():
        prefix = _built_in(locale)["invocationName"] or ""
        command = verification.MA_START_COMMANDS.get(
            locale, verification.MA_DEFAULT_START_COMMAND
        )
        if not prefix or not command.startswith(prefix):
            continue  # name-only launch (fr) or trailing invocation (nl)
        tail = command[len(prefix) :].strip()
        assert tail in _play_audio_samples(locale), f"{locale}: missing {tail!r}"
        checked += 1
    assert checked >= 8  # de, en-* x5, es-ES, it, pt


def test_es_mx_offers_the_english_fallback_tail():
    # MA has no es-MX key and falls back to the English command (upstream
    # table gap; V12 reports it as a warning) — the Spanish model still has
    # to accept the English tail MA actually sends.
    samples = _play_audio_samples("es-MX")
    assert "play audio" in samples
    assert "reproduzca audio" in samples  # ready if MA adds an es-MX key


def test_built_in_prefixes_address_the_invocation():
    # MA composes pause/resume/stop as "<prefix> <utterance>"; an empty or
    # off-name prefix breaks every control command (the other half of #71).
    for locale in _locales():
        prefix = _built_in(locale)["invocationName"] or ""
        assert prefix, f"{locale}: empty built-in invocation prefix"
        assert verification._names_invocation(prefix, INVOCATION), locale
