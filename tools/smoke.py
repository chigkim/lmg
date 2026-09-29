# /// script
# dependencies = ["openai>=1.100"]
# ///
"""Drive gateway.py with the openai client: a tool round trip, a follow-up, streaming, and an image, on both APIs.

    uv run tools/smoke.py MODEL [IMAGE]
"""
import base64
import json
import sys

from openai import OpenAI

MODEL = sys.argv[1]
IMAGE = sys.argv[2] if len(sys.argv) > 2 else None
api = OpenAI(base_url="http://127.0.0.1:8797/v1", api_key="unused")
WEATHER = {"name": "get_weather", "description": "The weather in a city, right now.",
           "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}


def weather(city):  # the client runs the tool, not the gateway
    return json.dumps({"city": city, "sky": "violet", "temperature_c": 31})


def check(label, ok, detail):
    print(("PASS" if ok else "FAIL"), label, "--", detail)


def chat():
    messages = [{"role": "user", "content": "What is the weather in Oslo? Use the tool."}]
    tools = [{"type": "function", "function": WEATHER}]
    first = api.chat.completions.create(model=MODEL, messages=messages, tools=tools).choices[0]
    calls = first.message.tool_calls or []
    check("chat: tool call", first.finish_reason == "tool_calls" and calls, [c.function.arguments for c in calls])
    messages.append(first.message.model_dump(exclude_none=True))
    for c in calls:
        messages.append({"role": "tool", "tool_call_id": c.id, "content": weather(json.loads(c.function.arguments)["city"])})
    second = api.chat.completions.create(model=MODEL, messages=messages, tools=tools).choices[0]
    check("chat: uses the result", "violet" in (second.message.content or "").lower(), second.message.content)
    messages.append({"role": "assistant", "content": second.message.content})
    messages.append({"role": "user", "content": "Repeat the temperature as a bare number."})
    stream = api.chat.completions.create(model=MODEL, messages=messages, tools=tools, stream=True)
    text = "".join(c.choices[0].delta.content or "" for c in stream if c.choices)
    check("chat: streamed follow-up", "31" in text, text)


def responses():
    tools = [dict(WEATHER, type="function")]
    items = [{"role": "user", "content": "What is the weather in Lima? Use the tool."}]
    events = list(api.responses.create(model=MODEL, input=items, tools=tools, stream=True))
    done = [e.response for e in events if e.type == "response.completed"]
    calls = [o for o in done[0].output if o.type == "function_call"] if done else []
    check("responses: streamed tool call", calls, [c.arguments for c in calls])
    items += [o.model_dump(exclude_none=True) for o in done[0].output]
    for c in calls:
        items.append({"type": "function_call_output", "call_id": c.call_id,
                      "output": weather(json.loads(c.arguments)["city"])})
    reply = api.responses.create(model=MODEL, input=items, tools=tools)
    check("responses: uses the result", "violet" in reply.output_text.lower(), reply.output_text)


def image():
    url = "data:image/jpeg;base64," + base64.b64encode(open(IMAGE, "rb").read()).decode()
    ask = "Describe this image in one sentence."
    a = api.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": [
        {"type": "text", "text": ask}, {"type": "image_url", "image_url": {"url": url}}]}])
    check("chat: image", bool(a.choices[0].message.content), a.choices[0].message.content)
    b = api.responses.create(model=MODEL, input=[{"role": "user", "content": [
        {"type": "input_text", "text": ask}, {"type": "input_image", "image_url": url}]}])
    check("responses: image", bool(b.output_text), b.output_text)


for test in (chat, responses) + ((image,) if IMAGE else ()):
    try:
        test()
    except Exception as e:
        check(test.__name__, False, f"{type(e).__name__}: {e}")
