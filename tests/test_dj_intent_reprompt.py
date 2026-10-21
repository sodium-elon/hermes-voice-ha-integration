"""DJ clarification response must retain the native Alexa reprompt."""

from pathlib import Path
import yaml
from jinja2 import Environment


def test_dj_relay_forwards_clarification_reprompt():
    path = Path(__file__).parents[1] / "deploy/homeassistant/dj_yakkuza_intent.yaml"
    intent = yaml.safe_load(path.read_text())["intent_script"]["DJYakkuzaIntent"]
    assert intent["action"][0]["data"]["conversation_id"] == "alexa_dj_yakkuza"
    env = Environment()
    response = {
        "response": {
            "speech": {
                "plain": {
                    "speech": "Which artist should I avoid?",
                    "extra_data": {"reprompt": "Please name the artist."},
                }
            }
        }
    }
    assert (
        env.from_string(intent["speech"]["text"]).render(action_response=response)
        == "Which artist should I avoid?"
    )
    assert (
        env.from_string(intent["reprompt"]["text"]).render(action_response=response)
        == "Please name the artist."
    )
    response["response"]["speech"]["plain"]["extra_data"]["reprompt"] = ""
    assert (
        env.from_string(intent["reprompt"]["text"]).render(action_response=response)
        == ""
    )
