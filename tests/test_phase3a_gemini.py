"""Phase 3a: streamed assistant answers, lower thinking, and a Gemini error log that says why (never the key)."""

import logging
from types import SimpleNamespace as NS
from unittest import mock

import pytest

from ahead import resources
from ahead.pages import assistant


def _ev(kind, **fields):
    return NS(event_type=kind, **fields)


def _text(t):
    return _ev("step.delta", delta=NS(type="text", text=t))


class FakeClient:
    def __init__(self, events=None, error=None):
        self.events, self.error, self.sent = events or [], error, []
        self.interactions = self
        self.closed = False

    def create(self, **kwargs):
        self.sent.append(kwargs)
        if self.error:
            raise self.error
        return iter(self.events)

    def close(self):
        self.closed = True


class BadRequest(Exception):
    def __init__(self, body, code="400"):
        super().__init__("Error code: 400 - " + str(body))
        self.status_code, self.body = code, body


KEY_BODY = {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key. key=AIzaSECRET",
                      "status": "INVALID_ARGUMENT",
                      "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"}]}}


def test_stream_yields_only_the_visible_answer():
    client = FakeClient([_ev("interaction.created"), _ev("step.delta", delta=NS(type="thought_summary", text="hidden")),
                         _text("Hello "), _text("world"), _ev("interaction.completed", interaction=NS(status="completed"))])
    with mock.patch.object(resources, "gemini_client", return_value=client):
        assert list(resources.gemini_stream("hi")) == ["Hello ", "world"]
    assert client.closed
    sent = client.sent[0]
    assert sent["stream"] is True and sent["store"] is False
    assert sent["generation_config"]["thinking_level"] == "low"


def test_stream_raises_on_error_event_and_on_no_text():
    with mock.patch.object(resources, "gemini_client", return_value=FakeClient([_text("Hi"), _ev("error", error=NS(code="overloaded"))])):
        with pytest.raises(resources.GeminiStreamError) as caught:
            list(resources.gemini_stream("hi"))
    assert caught.value.status_code == "503"
    with mock.patch.object(resources, "gemini_client", return_value=FakeClient([_ev("interaction.completed", interaction=NS(status="completed"))])):
        with pytest.raises(ValueError):
            list(resources.gemini_stream("hi"))


def test_chat_uses_low_thinking_and_the_transcript():
    client = FakeClient([_text("ok")])
    with mock.patch.object(resources, "gemini_client", return_value=client):
        out = "".join(resources.chat_stream("SYSTEM", [{"role": "user", "text": "What is diabetes?"}]))
    assert out == "ok"
    sent = client.sent[0]
    assert sent["generation_config"]["thinking_level"] == "low"
    assert "What is diabetes?" in sent["input"] and sent["system_instruction"].startswith("SYSTEM")


def test_chat_falls_back_to_the_fast_model_only_before_any_text(monkeypatch):
    monkeypatch.setattr(resources, "GEMINI_FAST_MODEL", "fast-model")
    busy = BadRequest({"error": {"status": "UNAVAILABLE"}}, code="503")
    clients = [FakeClient(error=busy), FakeClient([_text("from fast")])]
    with mock.patch.object(resources, "gemini_client", side_effect=clients):
        assert "".join(resources.chat_stream("S", [{"role": "user", "text": "hi"}])) == "from fast"
    assert clients[1].sent[0]["model"] == "fast-model"
    # Part way through an answer there is no silent restart on another model.
    clients = [FakeClient([_text("half"), _ev("error", error=NS(code="overloaded"))]), FakeClient([_text("again")])]
    with mock.patch.object(resources, "gemini_client", side_effect=clients):
        with pytest.raises(resources.GeminiStreamError):
            "".join(resources.chat_stream("S", [{"role": "user", "text": "hi"}]))
    assert not clients[1].sent


def test_error_log_names_googles_reason_but_never_the_message_or_key(caplog):
    caplog.set_level(logging.WARNING)
    message = resources.gemini_error_message(BadRequest(KEY_BODY))
    logged = caplog.text
    assert "HTTP 400 (INVALID_ARGUMENT/API_KEY_INVALID)" in logged
    assert "AIza" not in logged and "not valid" not in logged
    assert "API key" in message and "GEMINI_API_KEY" in message and "AIza" not in message


def test_reason_ignores_anything_that_is_not_an_enum_token():
    odd = {"error": {"status": "invalid argument: key AIzaSECRET", "details": [{"reason": "lower_case"}, {"reason": "A" * 80}, "x"]}}
    assert resources.gemini_error_reason(BadRequest(odd)) == ""
    assert resources.gemini_error_reason(BadRequest("not json")) == ""
    assert resources.gemini_error_reason(Exception("plain")) == ""


def test_other_400s_keep_the_general_message():
    body = {"error": {"status": "INVALID_ARGUMENT", "details": [{"reason": "BAD_MODEL_CONFIG"}]}}
    assert resources.gemini_error_message(BadRequest(body)).startswith("Gemini rejected the request")


def test_page_stream_turns_failures_into_plain_messages():
    def fails_at_once():
        raise BadRequest(KEY_BODY)
        yield  # pragma: no cover
    assert "".join(assistant._safe_stream(fails_at_once())).startswith("Gemini did not accept the API key")

    def fails_part_way():
        yield "Diabetes is "
        raise resources.GeminiStreamError("overloaded")
    out = "".join(assistant._safe_stream(fails_part_way()))
    assert out.startswith("Diabetes is ") and "temporarily unavailable" in out


def test_first_piece_is_fetched_before_the_spinner_closes():
    pulled = []

    def gen():
        for p in ("a", "b"):
            pulled.append(p)
            yield p
    rest = assistant._first_and_rest(gen())
    assert pulled == ["a"]
    assert list(rest) == ["a", "b"]
    assert list(assistant._first_and_rest(iter([]))) == []


def test_reply_stream_uses_the_right_prompt():
    sent = []
    with mock.patch.object(assistant, "chat_stream", side_effect=lambda system, turns: sent.append(system) or iter(["ok"])):
        assistant._reply_stream([{"role": "user", "content": "Hi"}])
        assistant._reply_stream([{"role": "user", "content": "Hi"}], is_doctor=True)
    assert sent == [assistant.SYSTEM_PROMPT, assistant.DOCTOR_SYSTEM_PROMPT]


def test_food_photo_and_insights_use_low_thinking():
    from ahead.pages import food
    with mock.patch.object(food, "gemini_api_key", return_value="k"), \
         mock.patch.object(food, "gemini_interaction", return_value='{"foods": []}') as call:
        food.analyze_photo(b"img", "image/png")
    assert call.call_args.kwargs["thinking_level"] == "low"
    with mock.patch.object(resources, "gemini_interaction", return_value="x") as call:
        resources.generate_text("p")
    assert call.call_args.kwargs["thinking_level"] == "low"
