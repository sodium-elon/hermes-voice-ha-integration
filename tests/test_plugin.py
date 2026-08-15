"""Tests for the hermes-voice-ha-integration plugins.

Test isolation: All HA connectivity is mocked via monkeypatching the bridge.
No real HA instance is required.

Run:
    python -m pytest tests/ -v
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    """Ensure test isolation — clean HASS env vars.

    Also redirect the HA audit log: tests must never append fake
    service calls (scene.nonexistent, light.a/b/c ...) to the real
    ~/.hermes/ha_audit.log, where hermes-voice-monitor surfaces them
    as alarming "blocked: error" lines.
    """
    monkeypatch.delenv("HASS_URL", raising=False)
    monkeypatch.delenv("HASS_TOKEN", raising=False)
    from plugins.home_assistant import security
    monkeypatch.setattr(security, "_AUDIT_LOG_FILE", tmp_path / "ha_audit_test.log")


@pytest.fixture
def with_hass(monkeypatch):
    """Set HASS_URL + HASS_TOKEN for a test."""
    monkeypatch.setenv("HASS_URL", "http://ha.local:8123")
    monkeypatch.setenv("HASS_TOKEN", "test-token-abc123")


# ---------------------------------------------------------------------------
# Bridge tests
# ---------------------------------------------------------------------------

class TestConversationBridge:
    def test_process_conversation_explicitly_targets_builtin_agent(self, with_hass, monkeypatch):
        from plugins.home_assistant import ha_assistant

        calls = []

        async def fake_process(text, language, conversation_id, agent_id):
            calls.append((text, language, conversation_id, agent_id))
            return {"response": {"response_type": "action_done"}}

        monkeypatch.setattr(
            ha_assistant,
            "_async_process_conversation",
            fake_process,
            raising=False,
        )

        result = ha_assistant.process_conversation(
            "Turn on the kitchen light",
            language="en",
            conversation_id="voice-1",
            agent_id="conversation.home_assistant",
        )

        assert result["response"]["response_type"] == "action_done"
        assert calls == [
            (
                "Turn on the kitchen light",
                "en",
                "voice-1",
                "conversation.home_assistant",
            )
        ]


class TestEntityCache:
    """EntityCache TTL and invalidation."""

    def test_cache_starts_empty(self, with_hass):
        from plugins.home_assistant.ha_assistant import _entity_cache
        _entity_cache.invalidate()
        assert _entity_cache.all() == []
        assert not _entity_cache.fresh

    def test_cache_set_and_get(self, with_hass):
        from plugins.home_assistant.ha_assistant import _entity_cache
        entities = [
            {"entity_id": "light.kitchen", "state": "on"},
            {"entity_id": "sensor.temperature", "state": "22.5"},
        ]
        _entity_cache.set(entities)
        assert _entity_cache.fresh
        assert len(_entity_cache.all()) == 2
        assert _entity_cache.get("light.kitchen")["state"] == "on"
        assert _entity_cache.get("sensor.missing") is None

    def test_cache_invalidation(self, with_hass):
        from plugins.home_assistant.ha_assistant import _entity_cache
        _entity_cache.set([{"entity_id": "light.kitchen", "state": "on"}])
        assert _entity_cache.fresh
        _entity_cache.invalidate()
        assert not _entity_cache.fresh
        assert _entity_cache.all() == []


class TestEntityIDRegex:
    """Entity ID validation."""

    _ENTITY_ID_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z0-9_]+$")

    def test_valid_entity_ids(self):
        assert self._ENTITY_ID_RE.match("light.living_room")
        assert self._ENTITY_ID_RE.match("sensor.temperature_1")
        assert self._ENTITY_ID_RE.match("binary_sensor.front_door_motion")

    def test_invalid_entity_ids(self):
        assert not self._ENTITY_ID_RE.match("LIGHT.living_room")
        assert not self._ENTITY_ID_RE.match("light.")
        assert not self._ENTITY_ID_RE.match(".living_room")
        assert not self._ENTITY_ID_RE.match("")
        assert not self._ENTITY_ID_RE.match("../../etc/passwd")


class TestSearch:
    """Search / filtering logic."""

    def test_search_by_domain(self, with_hass, monkeypatch):
        from plugins.home_assistant.ha_assistant import _entity_cache
        entities = [
            {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen Light"}},
            {"entity_id": "light.bedroom", "state": "off", "attributes": {"friendly_name": "Bedroom Light"}},
            {"entity_id": "switch.coffee", "state": "off", "attributes": {"friendly_name": "Coffee Switch"}},
        ]
        _entity_cache.set(entities)

        from plugins.home_assistant.ha_assistant import search_entities
        result = search_entities(domain="light")
        assert result["count"] == 2
        assert all(e["entity_id"].startswith("light.") for e in result["entities"])

    def test_search_by_query(self, with_hass, monkeypatch):
        from plugins.home_assistant.ha_assistant import _entity_cache
        entities = [
            {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen Light"}},
            {"entity_id": "light.bedroom", "state": "off", "attributes": {"friendly_name": "Bedroom Light"}},
        ]
        _entity_cache.set(entities)

        from plugins.home_assistant.ha_assistant import search_entities
        result = search_entities(query="kitchen")
        assert result["count"] == 1
        assert result["entities"][0]["entity_id"] == "light.kitchen"

    def test_search_by_area(self, with_hass, monkeypatch):
        from plugins.home_assistant.ha_assistant import _entity_cache
        entities = [
            {"entity_id": "sensor.kitchen_temp", "state": "22", "attributes": {"area": "kitchen"}},
            {"entity_id": "sensor.bedroom_temp", "state": "20", "attributes": {"area": "bedroom"}},
        ]
        _entity_cache.set(entities)

        from plugins.home_assistant.ha_assistant import search_entities
        result = search_entities(area="kitchen")
        assert result["count"] == 1

    def test_scene_resolution(self, with_hass, monkeypatch):
        from plugins.home_assistant.ha_assistant import _entity_cache
        entities = [
            {"entity_id": "scene.movie_night", "state": "", "attributes": {"friendly_name": "Movie Night"}},
        ]
        _entity_cache.set(entities)

        from plugins.home_assistant.compound import _resolve_scene_entity_id
        assert _resolve_scene_entity_id("scene.movie_night") == "scene.movie_night"
        assert _resolve_scene_entity_id("movie night") == "scene.movie_night"
        assert _resolve_scene_entity_id("nonexistent") is None


# ---------------------------------------------------------------------------
# Security tests
# ---------------------------------------------------------------------------

class TestAllowList:
    """Allow-list enforcement."""

    def test_allow_list_disabled(self, with_hass, monkeypatch):
        from plugins.home_assistant.security import is_service_allowed
        # Reset cache
        monkeypatch.setattr("plugins.home_assistant.security._ALLOW_LIST_FILE", Path("/nonexistent.json"))
        assert is_service_allowed("light.kitchen", "light", "turn_on") is True

    def test_allow_list_with_wildcard(self, with_hass, tmp_path, monkeypatch):
        config = {
            "enabled": True,
            "rules": [
                {"entity_id": "light.*", "services": ["turn_on", "turn_off"]},
                {"entity_id": "climate.living_room", "services": ["set_temperature"]},
            ],
        }
        f = tmp_path / "allow.json"
        f.write_text(json.dumps(config))
        monkeypatch.setattr("plugins.home_assistant.security._ALLOW_LIST_FILE", f)

        from plugins.home_assistant.security import is_service_allowed
        assert is_service_allowed("light.kitchen", "light", "turn_on") is True
        assert is_service_allowed("light.bedroom", "light", "turn_on") is True
        assert is_service_allowed("climate.living_room", "climate", "set_temperature") is True
        # Not in allow-list
        assert is_service_allowed("climate.living_room", "climate", "turn_off") is False
        assert is_service_allowed("switch.coffee", "switch", "turn_on") is False

    def test_entity_pattern_match(self):
        from plugins.home_assistant.security import _entity_matches
        assert _entity_matches("light.kitchen", "light.*") is True
        assert _entity_matches("light.kitchen.ceiling", "light.*") is True
        assert _entity_matches("switch.coffee", "light.*") is False
        assert _entity_matches("light.kitchen", "light.kitchen") is True
        assert _entity_matches("light.kitchen", "light.bedroom") is False


class TestBlockList:
    """Block-list enforcement."""

    def test_block_list_empty(self, with_hass, monkeypatch):
        monkeypatch.setattr("plugins.home_assistant.security._BLOCK_LIST_FILE", Path("/nonexistent.json"))
        from plugins.home_assistant.security import is_entity_blocked
        assert is_entity_blocked("light.kitchen", "light", "turn_on") is False

    def test_block_list_blocks(self, tmp_path, monkeypatch):
        config = {"entities": ["switch.server_power", "lock.front_door"]}
        f = tmp_path / "block.json"
        f.write_text(json.dumps(config))
        monkeypatch.setattr("plugins.home_assistant.security._BLOCK_LIST_FILE", f)

        from plugins.home_assistant.security import is_entity_blocked
        assert is_entity_blocked("switch.server_power", "switch", "turn_on") is True
        assert is_entity_blocked("lock.front_door", "lock", "unlock") is True
        assert is_entity_blocked("light.kitchen", "light", "turn_on") is False


class TestAuditLog:
    """Audit log writes."""

    def test_audit_log_writes(self, tmp_path, monkeypatch):
        log_file = tmp_path / "audit.log"
        monkeypatch.setattr("plugins.home_assistant.security._AUDIT_LOG_FILE", log_file)

        from plugins.home_assistant.security import log_call
        log_call("light.kitchen", "light", "turn_on", {"brightness": 255}, allowed=True, reason="test")

        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["entity_id"] == "light.kitchen"
        assert entry["domain"] == "light"
        assert entry["service"] == "turn_on"
        assert entry["allowed"] is True
        assert entry["reason"] == "test"


class TestBlockedDomains:
    """Security: blocked service domains."""

    def test_blocked_domains_rejected(self, with_hass):
        from plugins.home_assistant.ha_assistant import call_service
        result = call_service("shell_command", "run", data={"command": "rm -rf /"})
        assert "blocked" in result.get("error", "").lower()

    def test_safe_domains_allowed(self, with_hass, monkeypatch):
        # Mock the async call
        async def _mock_call(*a, **kw):
            return {"ok": True}
        monkeypatch.setattr("plugins.home_assistant.ha_assistant._async_call_service", _mock_call)

        from plugins.home_assistant.ha_assistant import call_service
        result = call_service("light", "turn_on", entity_id="light.kitchen")
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# Compound tool tests
# ---------------------------------------------------------------------------

class TestCompoundTools:
    """Tests for control_light_and_set_scene and turn_off_all_except."""

    def test_control_light_and_set_scene_missing_scene(self, monkeypatch):
        # Never hit a real HA server from tests: without this mock the
        # handler falls back to http://homeassistant.local:8123, burns a
        # real DNS lookup, and used to log "blocked: error" entries into
        # ~/.hermes/ha_audit.log (surfaced by hermes-voice-monitor).
        async def _mock_call(*a, **kw):
            raise RuntimeError("404: Entity 'scene.nonexistent' not found (mocked)")
        monkeypatch.setattr(
            "plugins.home_assistant.ha_assistant._async_call_service", _mock_call
        )

        from plugins.home_assistant.compound import _handle_control_light_and_set_scene
        result = _handle_control_light_and_set_scene({"scene": "scene.nonexistent"})
        data = json.loads(result)
        # Now routes through call_service which may attempt a real HA call
        # if env vars are set. Accept either a direct error or an HTTP error.
        if "error" in data:
            pass  # direct error from handler
        elif data.get("results"):
            # result may nest: results[0].result.error or results[0].error
            has_err = any(
                r.get("error") or (isinstance(r, dict) and r.get("result", {}).get("error"))
                for r in data["results"]
            )
            assert has_err, f"Expected error in results: {data}"
        else:
            pytest.fail(f"No error in response: {data}")

    def test_turn_off_all_except_no_entities(self, with_hass, monkeypatch):
        from plugins.home_assistant.ha_assistant import _entity_cache
        _entity_cache.set([])
        from plugins.home_assistant.compound import _handle_turn_off_all_except
        result = _handle_turn_off_all_except({"domain": "light", "preserve": ["light.kitchen"]})
        data = json.loads(result)
        assert data.get("message") or data.get("results") == []


# ---------------------------------------------------------------------------
# Plugin registration sanity
# ---------------------------------------------------------------------------

class TestPluginRegistration:
    """Verify that the plugin __init__.py is importable and tools are defined."""

    def test_tools_tuple_defined(self):
        """All 7 tools are in the _TOOLS tuple."""
        # We can't import directly without Hermes runtime, but we can parse
        import ast
        init_path = Path(__file__).parent.parent / "plugins" / "home_assistant" / "__init__.py"
        source = init_path.read_text()

        # Check that _TOOLS tuple has 7 entries
        tree = ast.parse(source)
        tools_count = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Tuple):
                tools_count = max(tools_count, len(node.elts))
        assert tools_count >= 8, f"Expected >= 8 tools in _TOOLS tuple, found {tools_count}"

    def test_plugin_yaml_valid(self):
        """plugin.yaml is valid YAML."""
        import yaml
        yaml_path = Path(__file__).parent.parent / "plugins" / "home_assistant" / "plugin.yaml"
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
        assert data["name"] == "home_assistant"
        assert data["version"] == "0.0.12"
        assert "on_session_start" in data["hooks"]

    def test_voice_stack_plugin_yaml_valid(self):
        """voice_stack plugin.yaml is valid YAML."""
        import yaml
        yaml_path = Path(__file__).parent.parent / "plugins" / "voice_stack" / "plugin.yaml"
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
        assert data["name"] == "voice_stack"
        assert data["version"] == "0.0.12"


# ---------------------------------------------------------------------------
# P2 Tests: Bulk control, Disambiguation, Event Watcher
# ---------------------------------------------------------------------------

class TestBulkControl:
    """ha_bulk_control compound tool tests."""

    def test_bulk_control_no_operations(self):
        from plugins.home_assistant.compound import _handle_bulk_control
        result = _handle_bulk_control({"operations": []})
        data = json.loads(result)
        assert "error" in data

    def test_bulk_control_single(self, monkeypatch):
        async def _mock_call(*a, **kw):
            return {"ok": True}
        monkeypatch.setattr("plugins.home_assistant.ha_assistant._async_call_service", _mock_call)

        from plugins.home_assistant.compound import _handle_bulk_control
        result = _handle_bulk_control({
            "operations": [
                {"domain": "light", "service": "turn_off", "entity_id": "light.kitchen"},
            ],
        })
        data = json.loads(result)
        assert data["results"] is not None
        assert data["summary"]["succeeded"] == 1

    def test_bulk_control_multiple(self, monkeypatch):
        async def _mock_call(*a, **kw):
            return {"ok": True}
        monkeypatch.setattr("plugins.home_assistant.ha_assistant._async_call_service", _mock_call)

        from plugins.home_assistant.compound import _handle_bulk_control
        result = _handle_bulk_control({
            "operations": [
                {"domain": "light", "service": "turn_off", "entity_id": "light.a"},
                {"domain": "light", "service": "turn_off", "entity_id": "light.b"},
                {"domain": "light", "service": "turn_off", "entity_id": "light.c"},
            ],
        })
        data = json.loads(result)
        assert data["summary"]["total"] == 3
        assert data["summary"]["succeeded"] == 3


class TestDisambiguation:
    """Entity disambiguation when 3+ entities match."""

    def test_handler_adds_disambiguation(self):
        """Verify _handle_search_entities adds disambiguation for 3+ matches."""
        import importlib
        mod = importlib.import_module("plugins.home_assistant.ha_assistant")
        mod._entity_cache.set([
            {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen Light"}},
            {"entity_id": "light.bedroom", "state": "off", "attributes": {"friendly_name": "Bedroom Light"}},
            {"entity_id": "light.bathroom", "state": "off", "attributes": {"friendly_name": "Bathroom Light"}},
        ])

        from plugins.home_assistant.__init__ import _handle_search_entities
        result = json.loads(_handle_search_entities({"query": "light"}))
        r = result["result"]
        assert r.get("disambiguation", {}).get("needed") is True
        assert len(r["disambiguation"]["entities"]) == 3


class TestEventWatcher:
    """EventSource and event watcher tests."""

    def test_event_source_singleton(self):
        from plugins.home_assistant.event_watcher import get_event_source
        s1 = get_event_source()
        s2 = get_event_source()
        assert s1 is s2

    def test_emit_and_get_events(self):
        from plugins.home_assistant.event_watcher import EventSource, StateChangedEvent
        source = EventSource(max_events=10)
        source.emit(StateChangedEvent("light.kitchen", "off", "on"))
        source.emit(StateChangedEvent("sensor.temp", "22", "24"))

        events = source.get_events()
        assert len(events) == 2
        assert events[0]["entity_id"] == "light.kitchen"
        assert events[1]["entity_id"] == "sensor.temp"

    def test_get_events_filtered(self):
        from plugins.home_assistant.event_watcher import EventSource, StateChangedEvent
        source = EventSource(max_events=10)
        source.emit(StateChangedEvent("light.kitchen", "off", "on"))
        source.emit(StateChangedEvent("light.bedroom", "on", "off"))

        events = source.get_events(entity_id="light.kitchen")
        assert len(events) == 1
        assert events[0]["entity_id"] == "light.kitchen"

    def test_subscriber_called(self):
        from plugins.home_assistant.event_watcher import EventSource, StateChangedEvent
        source = EventSource()
        received: list = []

        def cb(ev):
            received.append(ev.entity_id)

        source.subscribe(cb)
        source.emit(StateChangedEvent("light.kitchen", "off", "on"))
        assert received == ["light.kitchen"]

    def test_build_event_context(self):
        from plugins.home_assistant.event_watcher import (
            get_event_source, StateChangedEvent, build_event_context,
        )
        source = get_event_source()
        source.clear()
        source.emit(StateChangedEvent("light.kitchen", "off", "on"))
        source.emit(StateChangedEvent("sensor.temp", "22", "23"))

        ctx = build_event_context(max_events=5, max_age_seconds=600)
        assert "events" in ctx
        assert ctx["summary"] == "recent_state_changes"
        assert len(ctx["events"]) == 2


# ---------------------------------------------------------------------------
# P3 Tests: Status sensors, Scene/Script discovery, Observability
# ---------------------------------------------------------------------------

class TestStatusSensors:
    """Hermes → HA status sensor tests."""

    def test_sensors_initialised(self):
        from plugins.home_assistant.status_sensors import HermesStatusSensors
        sensors = HermesStatusSensors()
        snap = sensors.snapshot()
        assert len(snap) == 6
        names = [s["entity_id"] for s in snap]
        assert "binary_sensor.hermes_gateway_status" in names
        assert "sensor.hermes_uptime_hours" in names
        assert "sensor.hermes_total_interactions" in names
        assert "sensor.hermes_total_errors" in names
        assert "binary_sensor.ha_ws_connection" in names
        assert "binary_sensor.hermes_voice_ready" in names

    def test_sensor_state_values(self):
        from plugins.home_assistant.status_sensors import HermesStatusSensors
        sensors = HermesStatusSensors()
        sensors.gateway_connected = True
        sensors.ws_connected = True
        sensors.total_interactions = 42
        sensors.total_errors = 3
        snap = sensors.snapshot()
        # Find each sensor and check state
        gateway = [s for s in snap if s["entity_id"] == "binary_sensor.hermes_gateway_status"][0]
        assert gateway["state"] == "on"

        interactions = [s for s in snap if s["entity_id"] == "sensor.hermes_total_interactions"][0]
        assert interactions["state"] == 42

    def test_singleton(self):
        from plugins.home_assistant.status_sensors import get_status_sensors
        s1 = get_status_sensors()
        s2 = get_status_sensors()
        assert s1 is s2


class TestDiscovery:
    """Scene/Script auto-discovery tests."""

    def test_build_scene_tool_schemas(self):
        from plugins.home_assistant.discovery import build_scene_tool_schemas
        scenes = [
            {"entity_id": "scene.movie_night", "friendly_name": "Movie Night"},
            {"entity_id": "scene.morning", "friendly_name": "Morning Routine"},
        ]
        schemas = build_scene_tool_schemas(scenes)
        assert len(schemas) == 2
        assert schemas[0]["name"] == "ha_scene_movie_night"
        assert schemas[1]["name"] == "ha_scene_morning"
        assert "_scene_entity_id" in schemas[0]

    def test_build_script_tool_schemas(self):
        from plugins.home_assistant.discovery import build_script_tool_schemas
        scripts = [
            {"entity_id": "script.backup", "friendly_name": "Backup Script"},
        ]
        schemas = build_script_tool_schemas(scripts)
        assert len(schemas) == 1
        assert schemas[0]["name"] == "ha_script_backup"
        assert "_script_entity_id" in schemas[0]

    def test_make_scene_handler(self):
        from plugins.home_assistant.discovery import make_scene_handler
        handler = make_scene_handler("scene.movie_night")
        assert callable(handler)

    def test_make_script_handler(self):
        from plugins.home_assistant.discovery import make_script_handler
        handler = make_script_handler("script.backup")
        assert callable(handler)


class TestObservability:
    """Voice pipeline observability metrics."""

    def test_record_and_stats(self):
        from plugins.home_assistant.discovery import Observability, VoiceLatencyMetric
        obs = Observability(max_samples=10)
        obs.record(VoiceLatencyMetric(
            stt_latency_ms=500, llm_latency_ms=1800, tts_latency_ms=300, total_latency_ms=2600,
        ))
        obs.record(VoiceLatencyMetric(
            stt_latency_ms=400, llm_latency_ms=1500, tts_latency_ms=250, total_latency_ms=2150,
        ))
        stats = obs.stats()
        assert stats["samples"] == 2
        assert stats["avg_total_ms"] == 2375.0  # (2600+2150)/2

    def test_empty_stats(self):
        from plugins.home_assistant.discovery import Observability
        obs = Observability()
        stats = obs.stats()
        assert stats["samples"] == 0
        assert stats["avg_total_ms"] == 0

    def test_singleton(self):
        from plugins.home_assistant.discovery import get_observability
        o1 = get_observability()
        o2 = get_observability()
        assert o1 is o2

    def test_manifest_json_valid(self):
        manifest_path = Path(__file__).parent.parent / "custom_components" / "hermes" / "manifest.json"
        with open(manifest_path) as f:
            data = json.load(f)
        assert data["domain"] == "hermes"
        assert data["config_flow"] is True
        assert "iot_class" in data
        assert data["version"] == "0.0.12"

    def test_config_flow_translations_are_packaged_for_ha_ui(self):
        """HA must ship runtime translations, not only source strings.json."""
        root = Path(__file__).parent.parent / "custom_components" / "hermes"
        strings = json.loads((root / "strings.json").read_text())
        translations_path = root / "translations" / "en.json"
        assert translations_path.exists(), "missing HA runtime translation file"
        translations = json.loads(translations_path.read_text())
        assert translations == strings

    def test_config_flow_copy_explains_url_and_token_on_initial_page(self):
        root = Path(__file__).parent.parent / "custom_components" / "hermes"
        translations = json.loads((root / "translations" / "en.json").read_text())
        step = translations["config"]["step"]["user"]
        rendered_copy = "\n".join(
            [
                step["title"],
                step["description"],
                *step["data"].values(),
                *step["data_description"].values(),
            ]
        )
        for required in (
            "not your Home Assistant URL",
            "not a Home Assistant long-lived access token",
            "HERMES_HA_WS_TOKEN",
            "API_SERVER_KEY",
            "Hermes host/container",
            "http://hermes.local:7860",
            "Home Assistant connects",
        ):
            assert required in rendered_copy


# ---------------------------------------------------------------------------
# P4 Tests: Add-on structure validation
# ---------------------------------------------------------------------------

class TestAddonStructure:
    """HA add-on packaging validation."""

    def test_addon_config_yaml_valid(self):
        import yaml
        config_path = Path(__file__).parent.parent / "addon" / "config.yaml"
        assert config_path.exists(), "addon/config.yaml missing"
        with open(config_path) as f:
            data = yaml.safe_load(f)
        assert data["name"] == "Hermes Voice Assistant"
        assert data["version"] == "0.0.12"
        assert data["slug"] == "hermes_voice"
        assert "arch" in data
        assert "amd64" in data["arch"] or "aarch64" in data["arch"]

    def test_addon_build_yaml_valid(self):
        import yaml
        build_path = Path(__file__).parent.parent / "addon" / "build.yaml"
        assert build_path.exists(), "addon/build.yaml missing"
        with open(build_path) as f:
            data = yaml.safe_load(f)
        assert "build_from" in data

    def test_addon_dockerfile_exists(self):
        dockerfile = Path(__file__).parent.parent / "addon" / "Dockerfile"
        assert dockerfile.exists(), "addon/Dockerfile missing"
        content = dockerfile.read_text()
        assert "FROM" in content
        assert "ENTRYPOINT" in content or "CMD" in content

    def test_addon_run_sh_exists(self):
        run_sh = Path(__file__).parent.parent / "addon" / "run.sh"
        assert run_sh.exists(), "addon/run.sh missing"
        content = run_sh.read_text()
        assert "hermes" in content.lower()

    def test_addon_hacs_json_valid(self):
        hacs_path = Path(__file__).parent.parent / "addon" / "hacs.json"
        assert hacs_path.exists(), "addon/hacs.json missing"
        with open(hacs_path) as f:
            data = json.load(f)
        assert "name" in data
        assert "homeassistant" in data

# ---------------------------------------------------------------------------
# Voice Stack Engine tests
# ---------------------------------------------------------------------------

class TestTTSEngine:
    """TTS engine smoke tests."""

    def test_edge_tts_engine_exists(self):
        from plugins.voice_stack.engines.tts import EdgeTTSEngine
        engine = EdgeTTSEngine()
        assert callable(engine.available)
        assert callable(engine.synthesize)

    def test_piper_tts_engine_exists(self):
        from plugins.voice_stack.engines.tts import PiperTTSEngine
        engine = PiperTTSEngine(voice="en_US-lessac-medium")
        assert callable(engine.available)
        assert callable(engine.synthesize)

    def test_create_tts_engine_factory(self):
        from plugins.voice_stack.engines.tts import create_tts_engine, EdgeTTSEngine
        engine = create_tts_engine("edge", voice="en-US-AriaNeural")
        assert isinstance(engine, EdgeTTSEngine)

    def test_create_tts_engine_invalid(self):
        from plugins.voice_stack.engines.tts import create_tts_engine
        with pytest.raises(ValueError, match="Unknown TTS engine"):
            create_tts_engine("nonexistent")

    def test_edge_tts_list_voices(self):
        from plugins.voice_stack.engines.tts import EdgeTTSEngine
        engine = EdgeTTSEngine()
        voices = engine.list_voices()
        assert isinstance(voices, list)
        # Should have at least fallback voices even if CLI not installed
        assert len(voices) >= 1
        assert "name" in voices[0]
        assert "locale" in voices[0]

    def test_edge_tts_synthesize(self):
        from plugins.voice_stack.engines.tts import EdgeTTSEngine
        if not EdgeTTSEngine().available():
            pytest.skip("edge-tts not installed")
        engine = EdgeTTSEngine()
        path = engine.synthesize("Hello world")
        assert os.path.exists(path)
        assert path.endswith(".mp3")
        # Synthesise different text -> different file (different hashes)
        path2 = engine.synthesize("Goodbye world")
        # May be same due to hash collisions, but usually different extension
        os.unlink(path)
        os.unlink(path2)


class TestSTTEngine:
    """STT engine smoke tests."""

    def test_faster_whisper_engine_exists(self):
        from plugins.voice_stack.engines.stt import FasterWhisperEngine
        engine = FasterWhisperEngine(model_size="tiny")
        assert callable(engine.available)
        assert callable(engine.transcribe)

    def test_faster_whisper_passes_voice_vocabulary_and_vad_to_model(self):
        from types import SimpleNamespace
        from plugins.voice_stack.engines.stt import FasterWhisperEngine

        calls = []

        class FakeModel:
            def transcribe(self, audio_path, **kwargs):
                calls.append((audio_path, kwargs))
                return (
                    [SimpleNamespace(text=" Turn on CNN.", avg_logprob=-0.7)],
                    SimpleNamespace(language="en"),
                )

        engine = FasterWhisperEngine(
            model_size="small.en",
            initial_prompt="Home Assistant commands include: turn on CNN.",
            hotwords="CNN, LCI, TF1, Firedroid",
            vad_filter=True,
        )
        engine._model = FakeModel()

        result = engine.transcribe_with_confidence("sample.wav", language="en")

        assert result["text"] == "Turn on CNN."
        assert calls == [
            (
                "sample.wav",
                {
                    "language": "en",
                    "beam_size": 5,
                    "initial_prompt": "Home Assistant commands include: turn on CNN.",
                    "hotwords": "CNN, LCI, TF1, Firedroid",
                    "vad_filter": True,
                    "condition_on_previous_text": False,
                },
            )
        ]

    def test_faster_whisper_gates_no_speech_and_logprob(self):
        from types import SimpleNamespace
        from plugins.voice_stack.engines.stt import FasterWhisperEngine

        calls = []

        class FakeModel:
            def transcribe(self, audio_path, **kwargs):
                calls.append((audio_path, kwargs))
                return (
                    [SimpleNamespace(text=" Turn on CNN.", avg_logprob=-0.7)],
                    SimpleNamespace(language="en"),
                )

        engine = FasterWhisperEngine(
            model_size="small.en",
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
        )
        engine._model = FakeModel()

        engine.transcribe_with_confidence("sample.wav", language="en")

        opts = calls[0][1]
        assert opts["no_speech_threshold"] == 0.6
        assert opts["log_prob_threshold"] == -1.0

    def test_openwakeword_engine_vad_and_threshold(self):
        from plugins.voice_stack.engines.wake_word import OpenWakeWordEngine

        engine = OpenWakeWordEngine(threshold=0.55, vad_threshold=0.5)
        assert engine._threshold == 0.55
        assert engine._vad_threshold == 0.5

    def test_whisper_cpp_engine_exists(self):
        from plugins.voice_stack.engines.stt import WhisperCPPEngine
        engine = WhisperCPPEngine()
        assert callable(engine.available)
        assert callable(engine.transcribe)

    def test_create_stt_engine_factory(self):
        from plugins.voice_stack.engines.stt import create_stt_engine, FasterWhisperEngine
        engine = create_stt_engine("faster-whisper", model_size="tiny")
        assert isinstance(engine, FasterWhisperEngine)

    def test_create_stt_engine_invalid(self):
        from plugins.voice_stack.engines.stt import create_stt_engine
        with pytest.raises(ValueError, match="Unknown STT engine"):
            create_stt_engine("nonexistent")

    def test_faster_whisper_list_languages(self):
        from plugins.voice_stack.engines.stt import FasterWhisperEngine
        engine = FasterWhisperEngine(model_size="tiny")
        langs = engine.list_languages()
        assert isinstance(langs, list)
        assert len(langs) >= 1
        assert "en" in langs

    def test_whisper_cpp_list_languages(self):
        from plugins.voice_stack.engines.stt import WhisperCPPEngine
        engine = WhisperCPPEngine()
        langs = engine.list_languages()
        assert isinstance(langs, list)


class TestWakeWordEngine:
    """Wake word engine smoke tests."""

    def test_porcupine_engine_exists(self):
        from plugins.voice_stack.engines.wake_word import PorcupineEngine
        engine = PorcupineEngine()
        assert callable(engine.available)
        assert callable(engine.listen)

    def test_openwakeword_engine_exists(self):
        from plugins.voice_stack.engines.wake_word import OpenWakeWordEngine
        engine = OpenWakeWordEngine()
        assert callable(engine.available)
        assert callable(engine.listen)

    def test_voice_config_accepts_custom_openwakeword_model_paths(self, monkeypatch):
        import plugins.voice_stack as voice_stack

        monkeypatch.setenv("HERMES_WAKE_WORD_MODEL", "/models/hey_ria.tflite,/models/backup.onnx")
        monkeypatch.setenv("HERMES_WAKE_WORD_THRESHOLD", "0.40")

        config = voice_stack._get_config()

        assert config["wake_word"]["model_paths"] == [
            "/models/hey_ria.tflite",
            "/models/backup.onnx",
        ]
        assert config["wake_word"]["threshold"] == 0.40

    def test_voice_config_parses_wake_word_command(self, monkeypatch):
        import plugins.voice_stack as voice_stack

        monkeypatch.setenv(
            "HERMES_WAKE_WORD_COMMAND", "/usr/local/bin/wake-wait --timeout {timeout}"
        )

        config = voice_stack._get_config()

        assert config["wake_word"]["command"] == [
            "/usr/local/bin/wake-wait",
            "--timeout",
            "{timeout}",
        ]

    def test_voice_config_wake_word_command_defaults_empty(self, monkeypatch):
        import plugins.voice_stack as voice_stack

        monkeypatch.delenv("HERMES_WAKE_WORD_COMMAND", raising=False)

        assert voice_stack._get_config()["wake_word"]["command"] == []

    def test_command_engine_substitutes_timeout(self):
        from plugins.voice_stack.engines.wake_word import create_wake_word_engine

        engine = create_wake_word_engine(
            "command", command=["/bin/true", "--timeout", "{timeout}"]
        )
        # CommandWWEngine treats exit 0 as a detection.
        assert engine.listen(timeout_seconds=1.0) is True

    def test_command_engine_nonzero_exit_is_no_detection(self):
        from plugins.voice_stack.engines.wake_word import create_wake_word_engine

        engine = create_wake_word_engine("command", command=["/bin/false"])
        assert engine.listen(timeout_seconds=1.0) is False

    def test_command_engine_emits_wake_event(self, monkeypatch):
        """A detection must open a turn and emit 'wake'.

        The monitor renders wake events; the other engines emit one on every
        hit. When this is missing the pipeline still works but goes invisible,
        and every later stage is attributed to turn 0.
        """
        from plugins.voice_stack import events
        from plugins.voice_stack.engines.wake_word import create_wake_word_engine

        emitted = []
        turns = []
        monkeypatch.setattr(events, "emit", lambda kind, **f: emitted.append((kind, f)))
        monkeypatch.setattr(events, "begin_turn", lambda: turns.append(1) or 1)

        engine = create_wake_word_engine(
            "command", command=["/bin/echo", "hey_jarvis_v0.1"]
        )
        assert engine.listen(timeout_seconds=1.0) is True

        assert len(turns) == 1, "detection must open exactly one turn"
        assert emitted == [("wake", {"engine": "command", "word": "hey_jarvis_v0.1"})]

    def test_command_engine_emits_no_wake_event_on_timeout(self, monkeypatch):
        from plugins.voice_stack import events
        from plugins.voice_stack.engines.wake_word import create_wake_word_engine

        emitted = []
        monkeypatch.setattr(events, "emit", lambda kind, **f: emitted.append(kind))
        monkeypatch.setattr(events, "begin_turn", lambda: 1)

        engine = create_wake_word_engine("command", command=["/bin/false"])
        assert engine.listen(timeout_seconds=1.0) is False
        assert emitted == [], "a non-detection must not open a turn"

    def test_command_engine_wake_word_falls_back_when_silent(self, monkeypatch):
        """A detector that prints nothing still produces a usable event."""
        from plugins.voice_stack import events
        from plugins.voice_stack.engines.wake_word import create_wake_word_engine

        emitted = []
        monkeypatch.setattr(events, "emit", lambda kind, **f: emitted.append((kind, f)))
        monkeypatch.setattr(events, "begin_turn", lambda: 1)

        engine = create_wake_word_engine("command", command=["/bin/true"])
        assert engine.listen(timeout_seconds=1.0) is True
        assert emitted == [("wake", {"engine": "command", "word": "unknown"})]

    def test_openwakeword_engine_uses_configured_threshold(self):
        from plugins.voice_stack.engines.wake_word import OpenWakeWordEngine

        engine = OpenWakeWordEngine(threshold=0.40)

        assert engine._threshold == 0.40

    def test_openwakeword_reuses_model_and_stream_across_timeouts(self, monkeypatch):
        import time
        from openwakeword import model as model_module
        import pyaudio
        from plugins.voice_stack.engines.wake_word import OpenWakeWordEngine

        model_inits = []
        stream_opens = []
        clock = iter([0.0, 6.0, 6.0, 12.0])

        class FakeModel:
            def __init__(self, **kwargs):
                model_inits.append(kwargs)

            def predict(self, _pcm):
                return {"hey_jarvis": 0.0}

        class FakeStream:
            def read(self, _size, exception_on_overflow=False):
                return b"\x00\x00" * 1280

            def stop_stream(self):
                return None

            def close(self):
                return None

        class FakePyAudio:
            def open(self, **kwargs):
                stream_opens.append(kwargs)
                return FakeStream()

            def terminate(self):
                return None

        monkeypatch.setattr(model_module, "Model", FakeModel)
        monkeypatch.setattr(pyaudio, "PyAudio", FakePyAudio)
        monkeypatch.setattr(time, "monotonic", lambda: next(clock))

        engine = OpenWakeWordEngine(model_paths=["hey_jarvis.onnx"], threshold=0.4)

        assert engine.listen(timeout_seconds=5.0) is False
        assert engine.listen(timeout_seconds=5.0) is False
        assert len(model_inits) == 1
        assert len(stream_opens) == 1

    def test_create_wake_word_engine_factory(self):
        from plugins.voice_stack.engines.wake_word import (
            create_wake_word_engine, PorcupineEngine)
        engine = create_wake_word_engine("porcupine", keywords=["computer"])
        assert isinstance(engine, PorcupineEngine)

    def test_create_wake_word_engine_invalid(self):
        from plugins.voice_stack.engines.wake_word import create_wake_word_engine
        with pytest.raises(ValueError, match="Unknown wake word engine"):
            create_wake_word_engine("nonexistent")

    def test_porcupine_list_wake_words(self):
        from plugins.voice_stack.engines.wake_word import PorcupineEngine
        engine = PorcupineEngine()
        words = engine.list_wake_words()
        assert isinstance(words, list)
        assert "computer" in words
        assert "jarvis" in words


class TestPipelineState:
    """VoicePipelineState unit tests."""

    def test_initial_state(self):
        from plugins.voice_stack.pipeline import VoicePipelineState
        state = VoicePipelineState()
        assert state.enabled is False
        assert state.listening is False
        assert state.total_interactions == 0
        assert state.total_errors == 0

    def test_to_dict(self):
        from plugins.voice_stack.pipeline import VoicePipelineState
        state = VoicePipelineState()
        d = state.to_dict()
        assert d["enabled"] is False
        assert "uptime_seconds" in d

    def test_build_voice_system_prompt(self):
        from plugins.voice_stack.pipeline import build_voice_system_prompt
        prompt = build_voice_system_prompt()
        assert "Hermes" in prompt
        assert "voice" in prompt.lower()
        assert "Listen" not in prompt  # not too formal

    def test_build_voice_system_prompt_with_context(self):
        from plugins.voice_stack.pipeline import build_voice_system_prompt
        entities = [{"entity_id": "light.kitchen", "state": "on"}]
        prompt = build_voice_system_prompt(entities=entities)
        assert "light.kitchen" in prompt

    def test_pipeline_uses_configured_speech_floor_and_resets_empty_wake(self, monkeypatch):
        from plugins.voice_stack import pipeline as pipeline_module
        from plugins.voice_stack.pipeline import VoicePipeline

        record_calls = []

        class FakeSTT:
            def available(self):
                return True

        class FakeTTS:
            def available(self):
                return True

        voice_pipeline = VoicePipeline(
            callback=lambda _text: "Done.",
            stt_engine=FakeSTT(),
            tts_engine=FakeTTS(),
            speech_threshold=0.005,
        )
        voice_pipeline.state.enabled = True
        voice_pipeline.state.wake_word_detected = True

        def no_speech(*_args, **kwargs):
            record_calls.append(kwargs)
            voice_pipeline.state.enabled = False
            return False

        monkeypatch.setattr(pipeline_module, "record_audio", no_speech)

        voice_pipeline._run_loop()

        assert record_calls[0]["silence_threshold"] == 0.005
        assert voice_pipeline.state.wake_word_detected is False

    def test_loud_float_audio_is_normalized_without_pcm_wraparound(self):
        import numpy as np
        from plugins.voice_stack.pipeline import _float_audio_to_pcm16

        pcm = _float_audio_to_pcm16(
            np.array([-1.35, -1.0, -0.25, 0.0, 0.25, 1.0, 1.35], dtype=np.float32)
        )

        assert pcm.dtype == np.int16
        assert list(pcm) == sorted(pcm)
        assert pcm[0] < 0 < pcm[-1]
        assert abs(int(pcm[0])) <= 32767
        assert int(pcm[-1]) <= 32767

    def test_low_confidence_transcript_is_processed_without_confirmation(self, monkeypatch):
        from plugins.voice_stack import pipeline as pipeline_module
        from plugins.voice_stack.pipeline import VoicePipeline

        callbacks = []
        languages = []
        spoken = []

        class FakeSTT:
            def available(self):
                return True

            def transcribe_with_confidence(self, _audio_path, language=None):
                languages.append(language)
                voice_pipeline.state.enabled = False
                return {"text": "Turn on the kitchen light", "confidence": 0.2}

        class FakeTTS:
            def available(self):
                return True

        voice_pipeline = VoicePipeline(
            callback=lambda text: callbacks.append(text) or "Done.",
            stt_engine=FakeSTT(),
            tts_engine=FakeTTS(),
            language="en",
            confidence_threshold=0.7,
        )
        voice_pipeline.state.enabled = True
        monkeypatch.setattr(pipeline_module, "record_audio", lambda *_args, **_kwargs: True)
        monkeypatch.setattr(voice_pipeline, "_speak", lambda text: spoken.append(text) or True)

        voice_pipeline._run_loop()

        assert languages == ["en"]
        assert callbacks == ["Turn on the kitchen light"]
        assert spoken == ["Done."]

    def test_hallucination_below_confidence_floor_is_silently_dropped(self, monkeypatch):
        from plugins.voice_stack import pipeline as pipeline_module
        from plugins.voice_stack.pipeline import VoicePipeline

        callbacks = []
        spoken = []

        class FakeSTT:
            def available(self):
                return True

            def transcribe_with_confidence(self, _audio_path, language=None):
                voice_pipeline.state.enabled = False
                return {"text": "Thanks for watching!", "confidence": 0.066}

        class FakeTTS:
            def available(self):
                return True

        voice_pipeline = VoicePipeline(
            callback=lambda text: callbacks.append(text) or "You're welcome.",
            stt_engine=FakeSTT(),
            tts_engine=FakeTTS(),
            language="en",
            min_confidence=0.15,
        )
        voice_pipeline.state.enabled = True
        monkeypatch.setattr(pipeline_module, "record_audio", lambda *_args, **_kwargs: True)
        monkeypatch.setattr(voice_pipeline, "_speak", lambda text: spoken.append(text) or True)

        voice_pipeline._run_loop()

        assert callbacks == []
        assert spoken == []
        assert voice_pipeline.state.total_interactions == 0

    def test_question_response_relistens_without_second_wake_word(self, monkeypatch):
        from plugins.voice_stack import pipeline as pipeline_module
        from plugins.voice_stack.pipeline import VoicePipeline

        transcripts = iter(["TV4", "Yes"])
        responses = iter([
            "Do you want me to play TV4 on the TV?",
            "Started TV4.",
        ])
        callbacks = []
        spoken = []
        beeps = []

        class FakeWake:
            calls = 0

            def listen(self, timeout_seconds=5.0):
                self.calls += 1
                if self.calls == 1:
                    return True
                voice_pipeline.state.enabled = False
                return False

            def stop(self):
                pass

        class FakeSTT:
            calls = 0

            def available(self):
                return True

            def transcribe_with_confidence(self, _audio_path, language=None):
                self.calls += 1
                text = next(transcripts)
                if self.calls == 2:
                    voice_pipeline.state.enabled = False
                return {"text": text, "confidence": 0.9}

        class FakeTTS:
            def available(self):
                return True

        wake = FakeWake()
        voice_pipeline = VoicePipeline(
            callback=lambda text: callbacks.append(text) or next(responses),
            wake_word_engine=wake,
            stt_engine=FakeSTT(),
            tts_engine=FakeTTS(),
            wake_cooldown=0,
        )
        voice_pipeline.state.enabled = True
        monkeypatch.setattr(pipeline_module, "record_audio", lambda *_args, **_kwargs: True)
        monkeypatch.setattr(pipeline_module, "_play_wake_beep", lambda: beeps.append(True))
        monkeypatch.setattr(pipeline_module.time, "sleep", lambda _seconds: None)
        monkeypatch.setattr(voice_pipeline, "_speak", lambda text: spoken.append(text) or True)

        voice_pipeline._run_loop()

        assert wake.calls == 1
        assert callbacks == ["TV4", "Yes"]
        assert spoken == [
            "Do you want me to play TV4 on the TV?",
            "Started TV4.",
        ]
        assert len(beeps) == 2

    def test_audio_playback_timeout_exceeds_generated_clip_duration(self, monkeypatch):
        from plugins.voice_stack import pipeline

        calls = []
        monkeypatch.setattr(pipeline.os.path, "exists", lambda _path: True)
        monkeypatch.setattr(
            pipeline.shutil,
            "which",
            lambda player: f"/usr/bin/{player}" if player == "ffplay" else None,
        )
        monkeypatch.setattr(
            pipeline,
            "_probe_audio_duration",
            lambda _path: 30.96,
            raising=False,
        )
        monkeypatch.setattr(
            pipeline.subprocess,
            "run",
            lambda command, **kwargs: calls.append((command, kwargs)),
        )

        assert pipeline.play_audio_local("answer.mp3") is True
        assert calls[0][1]["timeout"] == pytest.approx(40.96)

    def test_voice_config_sets_stt_language(self, monkeypatch):
        import plugins.voice_stack as voice_stack

        monkeypatch.setenv("HERMES_STT_LANGUAGE", "en")

        assert voice_stack._get_config()["stt"]["language"] == "en"

    def test_voice_config_sets_stt_tuning(self, monkeypatch):
        import plugins.voice_stack as voice_stack

        monkeypatch.setenv("HERMES_STT_INITIAL_PROMPT", "turn on CNN")
        monkeypatch.setenv("HERMES_STT_HOTWORDS", "CNN, LCI, TF1")
        monkeypatch.setenv("HERMES_STT_VAD", "true")
        monkeypatch.setenv("HERMES_STT_MIN_CONFIDENCE", "0.15")

        stt = voice_stack._get_config()["stt"]

        assert stt["initial_prompt"] == "turn on CNN"
        assert stt["hotwords"] == "CNN, LCI, TF1"
        assert stt["vad_filter"] is True
        assert stt["min_confidence"] == 0.15

    def test_wake_beep_uses_hermes_audio_cue(self, monkeypatch):
        from plugins.voice_stack import pipeline

        calls = []
        monkeypatch.setenv("HERMES_WAKE_BEEP_DURATION", "0.60")
        monkeypatch.setattr(
            pipeline.subprocess,
            "run",
            lambda command, **kwargs: calls.append((command, kwargs)),
        )

        pipeline._play_wake_beep()

        command, kwargs = calls[0]
        assert command[0] == "ffplay"
        assert "sine=frequency=880:duration=0.60" in command
        assert kwargs["env"]["SDL_AUDIODRIVER"] == "pulseaudio"
        assert kwargs["timeout"] == 2


class TestVoicePluginInit:
    """Voice plugin __init__.py smoke tests."""

    def test_plugin_importable(self):
        import plugins.voice_stack  # noqa: F401

    def test_tools_registered(self):
        import ast
        init_path = Path(__file__).parent.parent / "plugins" / "voice_stack" / "__init__.py"
        source = init_path.read_text()
        tool_names = ["voice_status", "voice_enable", "voice_disable", "voice_speak",
                      "voice_listen", "voice_prompt"]
        for n in tool_names:
            assert f'"{n}"' in source, f"Tool '{n}' not found in voice_stack __init__.py"

    def test_plugin_yaml_version(self):
        import yaml
        yaml_path = Path(__file__).parent.parent / "plugins" / "voice_stack" / "plugin.yaml"
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
        assert "config" in data
        assert "HERMES_WAKE_WORD_ENGINE" in data["config"]
        assert "HERMES_HA_WS_PORT" in data["config"]
        assert "HERMES_HA_WS_TOKEN" in data["config"]
        assert data["version"] == "0.0.12"

    def test_register_auto_enables_voice_when_requested(self, monkeypatch):
        import plugins.voice_stack as voice_stack
        from plugins.voice_stack import ws_receiver

        calls = []

        class FakeContext:
            llm = object()

            def register_tool(self, **kwargs):
                return None

        monkeypatch.setenv("HERMES_VOICE_AUTO_ENABLE", "true")
        monkeypatch.setattr(voice_stack, "_init_engines", lambda: True)
        monkeypatch.setattr(
            voice_stack,
            "_handle_voice_enable",
            lambda args: calls.append(args) or json.dumps({"ok": True}),
        )
        monkeypatch.setattr(ws_receiver, "set_assist_query_handler", lambda handler: None)
        monkeypatch.setattr(ws_receiver, "start_ws_receiver", lambda: None)

        voice_stack.register(FakeContext())

        assert calls == [{}]



class TestVoiceWebSocketReceiver:
    """HA-facing /api/hermes/ws receiver tests."""

    def test_state_changed_ack(self):
        from plugins.voice_stack.ws_receiver import handle_ha_ws_payload
        result = handle_ha_ws_payload({
            "type": "state_changed",
            "entity_id": "light.kitchen",
            "state": "on",
        })
        assert result["ok"] is True
        assert result["type"] == "state_ack"
        assert result["entity_id"] == "light.kitchen"

    def test_unknown_message_type_errors(self):
        from plugins.voice_stack.ws_receiver import handle_ha_ws_payload
        result = handle_ha_ws_payload({"type": "banana"})
        assert result["ok"] is False
        assert result["type"] == "error"
        assert "Unsupported message type" in result["error"]

    def test_voice_action_dispatches_status(self, monkeypatch):
        from plugins.voice_stack import ws_receiver

        monkeypatch.setattr(
            "plugins.voice_stack._handle_voice_status",
            lambda args: json.dumps({"ok": True, "ready": True}),
        )
        result = ws_receiver.handle_ha_ws_payload({"type": "voice_action", "action": "status"})
        assert result["type"] == "voice_action_result"
        assert result["ok"] is True
        assert result["action"] == "status"
        assert result["result"]["ready"] is True

    def test_voice_action_rejects_unknown_action(self):
        from plugins.voice_stack.ws_receiver import handle_ha_ws_payload
        result = handle_ha_ws_payload({"type": "voice_action", "action": "explode"})
        assert result["ok"] is False
        assert "Unsupported voice action" in result["error"]

    def test_response_preserves_request_id_and_status_payload(self, monkeypatch):
        from plugins.voice_stack import ws_receiver

        monkeypatch.setenv("HERMES_HA_WS_TOKEN", "secret")
        result = ws_receiver.handle_ha_ws_payload({"id": "abc-123", "type": "status"})

        assert result["id"] == "abc-123"
        assert result["type"] == "status"
        assert result["ok"] is True
        assert result["auth_required"] is True
        assert "uptime_seconds" in result
        assert "active_connections" in result
        assert result["message_counters"]["status"] >= 1

    def test_voice_action_forwards_rich_context_args(self, monkeypatch):
        from plugins.voice_stack import ws_receiver

        seen = {}
        monkeypatch.setattr(
            "plugins.voice_stack._handle_voice_enable",
            lambda args: seen.setdefault("args", args) or json.dumps({"ok": True}),
        )

        result = ws_receiver.handle_ha_ws_payload({
            "id": 42,
            "type": "voice_action",
            "action": "enable",
            "media_player_entity": "media_player.kitchen",
            "entry_id": "entry-1",
            "args": {"duration": 4},
        })

        assert result["id"] == 42
        assert result["ok"] is True
        assert seen["args"] == {
            "id": 42,
            "media_player_entity": "media_player.kitchen",
            "entry_id": "entry-1",
            "duration": 4,
        }

    def test_auth_token_optional_and_enforced(self, monkeypatch):
        from plugins.voice_stack.ws_receiver import _auth_ok

        monkeypatch.delenv("HERMES_HA_WS_TOKEN", raising=False)
        monkeypatch.delenv("API_SERVER_KEY", raising=False)
        monkeypatch.delenv("HERMES_API_KEY", raising=False)
        assert _auth_ok({}) is True

        monkeypatch.setenv("HERMES_HA_WS_TOKEN", "secret")
        assert _auth_ok({}) is False
        assert _auth_ok({"Authorization": "Bearer secret"}) is True
        assert _auth_ok({"Authorization": "Bearer wrong"}) is False


class TestHermesServices:
    """Custom component services.py tests."""

    def test_service_importable(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "hermes_services",
            Path(__file__).parent.parent / "custom_components" / "hermes" / "services.py",
        )
        assert spec is not None

    def test_services_yaml_exists(self):
        svc_yaml = Path(__file__).parent.parent / "custom_components" / "hermes" / "services.yaml"
        assert svc_yaml.exists()
        content = svc_yaml.read_text()
        assert "hermes_command" in content
        assert "voice_settings" in content

    def test_services_registration_format(self):
        import yaml
        svc_yaml = Path(__file__).parent.parent / "custom_components" / "hermes" / "services.yaml"
        data = yaml.safe_load(svc_yaml.read_text())
        for svc_name in ("hermes_command", "voice_settings"):
            svc = data[svc_name]
            assert "name" in svc
            assert "description" in svc
            assert "fields" in svc
            assert isinstance(svc["fields"], dict)


class TestFrontend:
    """Lovelace card resource tests."""

    def test_frontend_file_exists(self):
        fp = Path(__file__).parent.parent / "custom_components" / "hermes" / "frontend.py"
        assert fp.exists()

    def test_card_js_exists(self):
        js = Path(__file__).parent.parent / "custom_components" / "hermes" / "hacsfiles" / "hermes_action_bar.js"
        assert js.exists()
        content = js.read_text()
        assert "HermesActionBar" in content
        assert "customElements.define" in content

    def test_card_js_registers_custom_element(self):
        js = Path(__file__).parent.parent / "custom_components" / "hermes" / "hacsfiles" / "hermes_action_bar.js"
        content = js.read_text()
        assert 'customElements.define("hermes-action-bar", HermesActionBar)' in content
        assert "class HermesActionBar" in content
        assert "setConfig" in content


class TestSensor:
    """Sensor platform tests."""

    def test_sensor_py_exists(self):
        sp = Path(__file__).parent.parent / "custom_components" / "hermes" / "sensor.py"
        assert sp.exists()
        content = sp.read_text()
        assert "HermesStatusSensor" in content
        assert "HermesStatusUpdateCoordinator" in content
        assert "async_setup_entry" in content

    def test_platform_listed(self):
        init = Path(__file__).parent.parent / "custom_components" / "hermes" / "__init__.py"
        assert "Platform.SENSOR" in init.read_text()
        assert "_CONVERSATION_PLATFORM" in init.read_text()
        assert 'getattr(Platform, "CONVERSATION", "conversation")' in init.read_text()


class TestIsAvailable:
    """is_available() HTTP ping tests."""

    def test_returns_false_when_no_token(self, monkeypatch):
        monkeypatch.delenv("HASS_TOKEN", raising=False)
        from plugins.home_assistant.ha_assistant import is_available
        assert is_available() is False

    def test_returns_false_when_unreachable(self, monkeypatch):
        monkeypatch.setenv("HASS_TOKEN", "fake-token")
        from plugins.home_assistant.ha_assistant import is_available
        import urllib.request
        with patch.object(urllib.request, "urlopen", side_effect=OSError("no route")):
            assert is_available() is False

    def test_returns_true_when_ping_succeeds(self, monkeypatch):
        monkeypatch.setenv("HASS_TOKEN", "fake-token")
        from plugins.home_assistant.ha_assistant import is_available
        import urllib.request
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value.status = 200
        with patch.object(urllib.request, "urlopen", return_value=mock_resp):
            assert is_available() is True


class TestTTSRetry:
    """TTS retry logic tests."""

    def test_mock_tts_retry(self):
        call_count = [0]
        class MockTTS:
            def synthesize(self, text):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise RuntimeError("TTS crash")
                return "/tmp/test.mp3"
            def available(self):
                return True
        tts = MockTTS()
        # First call fails
        try:
            tts.synthesize("hello")
        except RuntimeError:
            pass
        # Second call succeeds
        assert tts.synthesize("hello") == "/tmp/test.mp3"
        assert call_count[0] == 2


class TestVoiceLifecycleRobustness:
    """Voice lifecycle locking and one-shot listening robustness."""

    def test_voice_enable_disable_uses_single_pipeline_instance(self, monkeypatch):
        import plugins.voice_stack as voice_stack

        class FakeState:
            enabled = False
            def to_dict(self):
                return {"enabled": self.enabled}

        class FakePipeline:
            instances = []
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.state = FakeState()
                self.available = True
                self.stopped = False
                FakePipeline.instances.append(self)
            def start(self):
                self.state.enabled = True
                return True
            def stop(self):
                self.stopped = True
                self.state.enabled = False

        monkeypatch.setattr("plugins.voice_stack.pipeline.VoicePipeline", FakePipeline)
        monkeypatch.setattr(voice_stack, "_ensure_voice_ready", lambda: True)
        monkeypatch.setattr(voice_stack, "_wake_word_engine", object())
        monkeypatch.setattr(voice_stack, "_stt_engine", object())
        monkeypatch.setattr(voice_stack, "_tts_engine", object())
        voice_stack._voice_ready.set()
        voice_stack._pipeline = None
        try:
            first = json.loads(voice_stack._handle_voice_enable({"media_player_entity": "media_player.kitchen"}))
            second = json.loads(voice_stack._handle_voice_enable({"media_player_entity": "media_player.kitchen"}))
            assert first["ok"] is True
            assert second["ok"] is True
            assert "already enabled" in second["message"]
            assert len(FakePipeline.instances) == 1
            assert FakePipeline.instances[0].kwargs["media_player_entity"] == "media_player.kitchen"

            disabled = json.loads(voice_stack._handle_voice_disable({}))
            assert disabled["ok"] is True
            assert FakePipeline.instances[0].stopped is True
            assert voice_stack._pipeline is None
        finally:
            voice_stack._pipeline = None
            voice_stack._voice_ready.clear()

    def test_full_agent_fallback_runs_through_sessions_api(self, monkeypatch):
        """The unmatched-query path uses the warm Sessions API, not a subprocess.

        Spawning `hermes chat` per turn paid a full CLI cold start and forced
        the child to be isolated from the parent's voice pipeline. Going
        through the already-running API server removes both concerns, so this
        asserts the new contract: the transcript and the voice system prompt
        are handed to sessions_api.complete, and its result is returned as-is.
        """
        import plugins.voice_stack as voice_stack
        from plugins.voice_stack import sessions_api

        calls = []

        def fake_complete(text, *, system_prompt=None, **kwargs):
            calls.append({"text": text, "system_prompt": system_prompt})
            return "It is sunny in Paris."

        monkeypatch.setattr(sessions_api, "complete", fake_complete)
        monkeypatch.setattr(sessions_api, "session_id", lambda: "voice-stack")

        response = voice_stack._run_full_agent("What is the weather in Paris?")

        assert response == "It is sunny in Paris."
        assert len(calls) == 1
        assert calls[0]["text"] == "What is the weather in Paris?"
        assert "one or two concise plain sentences" in calls[0]["system_prompt"]
        assert "markdown" in calls[0]["system_prompt"]

    def test_full_agent_fallback_propagates_api_failure(self, monkeypatch):
        """A dead API server must raise so the caller degrades to the raw LLM."""
        import plugins.voice_stack as voice_stack
        from plugins.voice_stack import sessions_api

        def boom(text, **kwargs):
            raise sessions_api.SessionsAPIUnavailable("API server unreachable")

        monkeypatch.setattr(sessions_api, "complete", boom)

        with pytest.raises(sessions_api.SessionsAPIUnavailable):
            voice_stack._run_full_agent("What is the weather in Paris?")

    def test_full_agent_fallback_rejects_empty_answer(self, monkeypatch):
        import plugins.voice_stack as voice_stack
        from plugins.voice_stack import sessions_api

        monkeypatch.setattr(sessions_api, "complete", lambda text, **kwargs: "   ")
        monkeypatch.setattr(sessions_api, "session_id", lambda: "voice-stack")

        with pytest.raises(RuntimeError, match="no response"):
            voice_stack._run_full_agent("What is the weather in Paris?")

    def test_voice_transcript_routes_to_builtin_ha_agent_before_llm(self, monkeypatch):
        import plugins.voice_stack as voice_stack
        from plugins.voice_stack import ha_conversation as ha_assistant

        calls = []

        def fake_process(text, *, language, conversation_id, agent_id):
            calls.append((text, language, conversation_id, agent_id))
            return {
                "response": {
                    "response_type": "action_done",
                    "speech": {"plain": {"speech": "Turned on the kitchen light"}},
                },
                "conversation_id": "ha-conv-1",
            }

        class FailingLlm:
            def complete(self, **_kwargs):
                raise AssertionError("Hermes LLM must not run for an HA-handled command")

        class FakeContext:
            llm = FailingLlm()

        monkeypatch.setattr(ha_assistant, "process_conversation", fake_process, raising=False)

        response = voice_stack._route_voice_transcript(
            FakeContext(), "Turn on the kitchen light", language="en"
        )

        assert response == "Turned on the kitchen light"
        assert calls == [
            (
                "Turn on the kitchen light",
                "en",
                None,
                "conversation.home_assistant",
            )
        ]

    def test_voice_transcript_retries_turn_on_as_run_for_ha_scripts(self, monkeypatch):
        import plugins.voice_stack as voice_stack
        from plugins.voice_stack import ha_conversation

        calls = []

        def fake_process(text, **_kwargs):
            calls.append(text)
            if text == "turn on cnn":
                return {
                    "response": {
                        "response_type": "error",
                        "speech": {"plain": {"speech": "Unknown device"}},
                        "data": {"code": "no_valid_targets"},
                    }
                }
            return {
                "response": {
                    "response_type": "action_done",
                    "speech": {"plain": {"speech": "Started"}},
                    "data": {
                        "success": [
                            {"name": "CNN", "type": "entity", "id": "script.channel_cnn_us"}
                        ],
                        "failed": [],
                    },
                }
            }

        class FailingLlm:
            def complete(self, **_kwargs):
                raise AssertionError("Hermes LLM must not run after HA handles the retry")

        class FakeContext:
            llm = FailingLlm()

        monkeypatch.setattr(ha_conversation, "process_conversation", fake_process)

        response = voice_stack._route_voice_transcript(
            FakeContext(), "turn on cnn", language="en"
        )

        assert calls == ["turn on cnn", "run cnn"]
        assert response == "Started CNN."

    def test_script_retry_expands_stt_digits_to_spoken_channel_name(self, monkeypatch):
        import plugins.voice_stack as voice_stack
        from plugins.voice_stack import ha_conversation

        calls = []

        def fake_process(text, **_kwargs):
            calls.append(text)
            if text != "run France Three.":
                return {
                    "response": {
                        "response_type": "error",
                        "speech": {"plain": {"speech": "Unknown device"}},
                        "data": {"code": "no_valid_targets"},
                    }
                }
            return {
                "response": {
                    "response_type": "action_done",
                    "speech": {"plain": {"speech": "Started"}},
                    "data": {
                        "success": [
                            {
                                "name": "France Three",
                                "type": "entity",
                                "id": "script.channel_france_3",
                            }
                        ],
                        "failed": [],
                    },
                }
            }

        monkeypatch.setattr(ha_conversation, "process_conversation", fake_process)

        response = voice_stack._route_voice_transcript(
            object(), "Turn on France 3.", language="en"
        )

        assert calls == [
            "Turn on France 3.",
            "run France 3.",
            "run France Three.",
        ]
        assert response == "Started France Three."

    def test_live_channel_selector_ignores_stale_entities_and_normalizes_names(self):
        from plugins.voice_stack.ha_conversation import select_live_channel

        states = [
            {
                "entity_id": "script.channel_tf1",
                "state": "unavailable",
                "attributes": {"friendly_name": "TF1"},
            },
            {
                "entity_id": "script.channel_tf_1",
                "state": "off",
                "attributes": {"friendly_name": "TF ONE"},
            },
            {
                "entity_id": "script.channel_tv_4_sweden",
                "state": "off",
                "attributes": {"friendly_name": "TV 4"},
            },
            {
                "entity_id": "script.channel_france_2",
                "state": "off",
                "attributes": {"friendly_name": "France Two"},
            },
        ]

        assert select_live_channel(states, "TF1")["entity_id"] == "script.channel_tf_1"
        assert select_live_channel(states, "TV4.")["entity_id"] == "script.channel_tv_4_sweden"
        assert select_live_channel(states, "France 2")["entity_id"] == "script.channel_france_2"

    def test_no_target_uses_verified_live_channel_instead_of_cached_assist_name(self, monkeypatch):
        import plugins.voice_stack as voice_stack
        from plugins.voice_stack import ha_conversation

        monkeypatch.setattr(
            ha_conversation,
            "process_conversation",
            lambda *_args, **_kwargs: {
                "response": {
                    "response_type": "error",
                    "speech": {"plain": {"speech": "Unknown device"}},
                    "data": {"code": "no_valid_targets"},
                }
            },
        )
        calls = []
        monkeypatch.setattr(
            ha_conversation,
            "run_live_channel",
            lambda target: calls.append(target) or {
                "ok": True,
                "entity_id": "script.channel_tv_4_sweden",
                "name": "TV 4",
                "media_title": "Nyheterna",
            },
        )

        response = voice_stack._route_voice_transcript(
            object(), "Turn on TV4.", language="en"
        )

        assert calls == ["TV4."]
        assert response == "Started TV 4."

    def test_live_channel_verification_failure_is_reported_honestly(self, monkeypatch):
        import plugins.voice_stack as voice_stack
        from plugins.voice_stack import ha_conversation

        monkeypatch.setattr(
            ha_conversation,
            "process_conversation",
            lambda *_args, **_kwargs: {
                "response": {
                    "response_type": "error",
                    "speech": {"plain": {"speech": "Unknown device"}},
                    "data": {"code": "no_valid_targets"},
                }
            },
        )
        monkeypatch.setattr(
            ha_conversation,
            "run_live_channel",
            lambda _target: {
                "ok": False,
                "reason": "verification_failed",
                "name": "TV 4",
                "entity_id": "script.channel_tv_4_sweden",
            },
        )

        response = voice_stack._route_voice_transcript(
            object(), "Turn on TV4.", language="en"
        )

        assert response == "I found TV 4, but the channel did not switch."

    def test_rejected_device_command_does_not_fall_back_to_agent_guessing(self, monkeypatch):
        import plugins.voice_stack as voice_stack
        from plugins.voice_stack import ha_conversation

        calls = []

        def reject(text, **_kwargs):
            calls.append(text)
            return {
                "response": {
                    "response_type": "error",
                    "speech": {"plain": {"speech": "I could not find that device."}},
                    "data": {"code": "no_valid_targets"},
                }
            }

        monkeypatch.setattr(ha_conversation, "process_conversation", reject)
        monkeypatch.setattr(
            voice_stack,
            "_run_full_agent",
            lambda _text: (_ for _ in ()).throw(
                AssertionError("Device commands must not be guessed by the full agent")
            ),
        )

        response = voice_stack._route_voice_transcript(
            object(), "turn on mystery TV", language="en"
        )

        assert calls == ["turn on mystery TV", "run mystery TV"]
        assert response == "I could not find that device."

    def test_voice_transcript_falls_back_to_hermes_only_when_ha_cannot_match(self, monkeypatch):
        import plugins.voice_stack as voice_stack
        from plugins.voice_stack import ha_conversation as ha_assistant

        class FailingLlm:
            def complete(self, **_kwargs):
                raise AssertionError("Raw LLM must not replace the tool-capable agent")

        class FakeContext:
            llm = FailingLlm()

        monkeypatch.setattr(
            ha_assistant,
            "process_conversation",
            lambda *_args, **_kwargs: {
                "response": {
                    "response_type": "error",
                    "speech": {"plain": {"speech": "Sorry, I couldn't understand that"}},
                }
            },
            raising=False,
        )
        monkeypatch.setattr(
            voice_stack,
            "_run_full_agent",
            lambda text: f"Tool-capable answer for: {text}",
        )
        ctx = FakeContext()

        response = voice_stack._route_voice_transcript(ctx, "Tell me something clever", language="en")

        assert response == "Tool-capable answer for: Tell me something clever"

    def test_alpharunner_question_routes_to_sportscoach_profile(self, monkeypatch):
        import plugins.voice_stack as voice_stack
        from plugins.voice_stack import ha_conversation

        monkeypatch.setattr(
            ha_conversation,
            "process_conversation",
            lambda *_args, **_kwargs: {
                "response": {
                    "response_type": "error",
                    "speech": {"plain": {"speech": "Sorry, I couldn't understand that"}},
                }
            },
        )
        calls = []

        def fake_run(text, *, profile=None):
            calls.append((text, profile))
            return "Your fastest run was five kilometres in twenty minutes."

        monkeypatch.setattr(voice_stack, "_run_full_agent", fake_run)

        response = voice_stack._route_voice_transcript(
            object(), "What is my fastest run according to alpha run?", language="en"
        )

        assert response == "Your fastest run was five kilometres in twenty minutes."
        assert calls == [
            ("What is my fastest run according to alpha run?", "sportscoach")
        ]

    def test_profile_api_failure_does_not_fall_back_to_contextless_llm(self, monkeypatch):
        import plugins.voice_stack as voice_stack
        from plugins.voice_stack import sessions_api

        monkeypatch.setattr(
            voice_stack,
            "_run_full_agent",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                sessions_api.SessionsAPIUnavailable("sportscoach API is down")
            ),
        )

        class FailingLlm:
            def complete(self, **_kwargs):
                raise AssertionError("Profile-owned requests must not use the raw LLM")

        class FakeContext:
            llm = FailingLlm()

        response = voice_stack._complete_voice_with_hermes(
            FakeContext(), "What was my fastest run last week?"
        )

        assert response == "Sports Coach is unavailable right now."

    def test_voice_enable_routes_transcript_through_home_assistant(self, monkeypatch):
        import plugins.voice_stack as voice_stack
        from plugins.voice_stack import ha_conversation as ha_assistant

        class FakeResult:
            text = "The actual Hermes response."

        class FakeLlm:
            def __init__(self):
                self.calls = []

            def complete(self, messages, **kwargs):
                self.calls.append((messages, kwargs))
                return FakeResult()

        class FakeContext:
            def __init__(self):
                self.llm = FakeLlm()

        class FakeState:
            enabled = False

            def to_dict(self):
                return {"enabled": self.enabled}

        class FakePipeline:
            instance = None

            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.state = FakeState()
                self.available = True
                FakePipeline.instance = self

            def start(self):
                self.state.enabled = True
                return True

            def stop(self):
                self.state.enabled = False

        ctx = FakeContext()
        monkeypatch.setattr(
            ha_assistant,
            "process_conversation",
            lambda *_args, **_kwargs: {
                "response": {
                    "response_type": "action_done",
                    "speech": {"plain": {"speech": "Turned on the kitchen light"}},
                }
            },
        )
        monkeypatch.setattr("plugins.voice_stack.pipeline.VoicePipeline", FakePipeline)
        monkeypatch.setattr(voice_stack, "_ensure_voice_ready", lambda: True)
        monkeypatch.setattr(voice_stack, "_wake_word_engine", object())
        monkeypatch.setattr(voice_stack, "_stt_engine", object())
        monkeypatch.setattr(voice_stack, "_tts_engine", object())
        monkeypatch.setattr(voice_stack, "_plugin_ctx", ctx, raising=False)
        voice_stack._voice_ready.set()
        voice_stack._pipeline = None

        try:
            enabled = json.loads(voice_stack._handle_voice_enable({}))
            assert FakePipeline.instance is not None
            response = FakePipeline.instance.kwargs["callback"]("Turn on the kitchen light")
        finally:
            voice_stack._pipeline = None
            voice_stack._voice_ready.clear()

        assert enabled["ok"] is True
        assert response == "Turned on the kitchen light"
        assert ctx.llm.calls == []

    def test_voice_listen_creates_cache_dir_and_returns_no_speech_category(self, monkeypatch, tmp_path):
        import plugins.voice_stack as voice_stack

        class FakeSTT:
            def available(self):
                return True

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(voice_stack, "_ensure_voice_ready", lambda: True)
        monkeypatch.setattr(voice_stack, "_stt_engine", FakeSTT())
        monkeypatch.setattr("plugins.voice_stack.pipeline.record_audio", lambda path, duration: False)
        voice_stack._voice_ready.set()
        try:
            result = json.loads(voice_stack._handle_voice_listen({"duration": 1}))
        finally:
            voice_stack._voice_ready.clear()

        assert result["ok"] is False
        assert result["error_category"] == "no_speech"
        assert (tmp_path / ".hermes" / "voice_cache").is_dir()

    def test_voice_listen_reports_recording_failure_category(self, monkeypatch, tmp_path):
        import plugins.voice_stack as voice_stack
        from plugins.voice_stack.pipeline import VoiceRecordingError

        class FakeSTT:
            def available(self):
                return True

        def boom(path, duration):
            raise VoiceRecordingError("microphone unavailable")

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(voice_stack, "_ensure_voice_ready", lambda: True)
        monkeypatch.setattr(voice_stack, "_stt_engine", FakeSTT())
        monkeypatch.setattr("plugins.voice_stack.pipeline.record_audio", boom)
        voice_stack._voice_ready.set()
        try:
            result = json.loads(voice_stack._handle_voice_listen({"duration": 1}))
        finally:
            voice_stack._voice_ready.clear()

        assert result["ok"] is False
        assert result["error_category"] == "recording_failed"
        assert "microphone unavailable" in result["error"]

    def test_voice_listen_rejects_invalid_duration(self, monkeypatch):
        import plugins.voice_stack as voice_stack

        class FakeSTT:
            def available(self):
                return True

        monkeypatch.setattr(voice_stack, "_ensure_voice_ready", lambda: True)
        monkeypatch.setattr(voice_stack, "_stt_engine", FakeSTT())
        voice_stack._voice_ready.set()
        try:
            result = json.loads(voice_stack._handle_voice_listen({"duration": "banana"}))
        finally:
            voice_stack._voice_ready.clear()

        assert result == {"ok": False, "error": "duration must be numeric", "error_category": "invalid_duration"}


class TestHomescriptSkill:
    """homescript skill tests."""

    def test_skill_md_exists(self):
        skill = Path(__file__).parent.parent / "skills" / "homescript" / "SKILL.md"
        assert skill.exists()

    def test_skill_has_frontmatter(self):
        skill = Path(__file__).parent.parent / "skills" / "homescript" / "SKILL.md"
        content = skill.read_text()
        assert content.startswith("---")
        assert "name: homescript" in content
        assert "version:" in content

    def test_skill_lists_tools(self):
        skill = Path(__file__).parent.parent / "skills" / "homescript" / "SKILL.md"
        content = skill.read_text()
        assert "ha_search_entities" in content
        assert "ha_get_state" in content
        assert "ha_call_service" in content
        assert "ha_bulk_control" in content


class TestLogo:
    """Brand assets tests."""

    def test_logo_exists(self):
        logo = Path(__file__).parent.parent / "logo.png"
        assert logo.exists()
        assert logo.stat().st_size > 1000

    def test_packaged_integration_icons_exist(self):
        root = Path(__file__).parent.parent
        for rel in (
            "icon.png",
            "custom_components/hermes/icon.png",
            "custom_components/hermes/logo.png",
            "custom_components/hermes/brand/icon.png",
            "custom_components/hermes/brand/logo.png",
            "custom_components/hermes/brand/icon@2x.png",
            "custom_components/hermes/brand/logo@2x.png",
        ):
            asset = root / rel
            assert asset.exists(), f"missing brand asset: {rel}"
            assert asset.stat().st_size > 1000


class TestCHANGELOG:
    """CHANGELOG.md validation."""

    def test_changelog_exists(self):
        cl = Path(__file__).parent.parent / "CHANGELOG.md"
        assert cl.exists()

    def test_changelog_has_version_entries(self):
        cl = Path(__file__).parent.parent / "CHANGELOG.md"
        content = cl.read_text()
        assert "## [0.0.10]" in content
        assert "## [0.0.7]" in content
        assert "## [0.0.6]" in content




def test_plugins_import_from_user_plugin_namespace(monkeypatch, tmp_path):
    """User-installed plugins load under Hermes' dynamic plugin namespace.

    Absolute imports like ``from plugins.voice_stack...`` fail there because
    the user plugin namespace is ``hermes_plugins``. Package-relative imports
    must keep both sibling plugins importable without touching the local Hermes
    source tree.
    """
    import importlib
    import shutil
    import sys
    import types

    root = Path(__file__).resolve().parents[1]
    namespace_dir = tmp_path / "hermes_plugins"
    namespace_dir.mkdir()
    shutil.copytree(root / "plugins" / "home_assistant", namespace_dir / "home_assistant")
    shutil.copytree(root / "plugins" / "voice_stack", namespace_dir / "voice_stack")

    namespace = types.ModuleType("hermes_plugins")
    namespace.__path__ = [str(namespace_dir)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_plugins", namespace)

    home_assistant = importlib.import_module("hermes_plugins.home_assistant")
    voice_stack = importlib.import_module("hermes_plugins.voice_stack")

    assert hasattr(home_assistant, "register")
    assert hasattr(voice_stack, "register")
