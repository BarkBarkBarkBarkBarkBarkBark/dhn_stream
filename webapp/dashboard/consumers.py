"""
WebSocket consumer for the DHN dashboard.

Handles commands from the browser (start/stop sender/receiver) and
forwards group messages (stats, spectrum) back to the browser as JSON.
"""

from __future__ import annotations

import json

from channels.generic.websocket import AsyncWebsocketConsumer

from dashboard import state
from dashboard.sine_sender import run_sine_sender
from dashboard.si_sender import run_si_sender
from dashboard.spectrum_decoder import run_spectrum_decoder


class DashboardConsumer(AsyncWebsocketConsumer):
    GROUP = "dashboard"

    async def connect(self) -> None:
        await self.channel_layer.group_add(self.GROUP, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code: int) -> None:
        await self.channel_layer.group_discard(self.GROUP, self.channel_name)

    async def receive(self, text_data: str) -> None:
        try:
            msg = json.loads(text_data)
        except json.JSONDecodeError:
            await self.send(json.dumps({"error": "invalid JSON"}))
            return

        cmd = msg.get("cmd", "")
        config = msg.get("config", {})

        if cmd == "start_sender":
            source    = config.get("source", "sine_harmonics")
            sender_fn = run_si_sender if source == "spikeinterface" else run_sine_sender
            state.sender_state.start(target=sender_fn, kwargs={"config": config, "stop_event": state.sender_state.stop_event})
            await self.send(json.dumps({"ack": "sender_started"}))

        elif cmd == "stop_sender":
            state.sender_state.stop()
            await self.send(json.dumps({"ack": "sender_stopped"}))

        elif cmd == "start_receiver":
            state.receiver_state.start(target=run_spectrum_decoder, kwargs={"config": config, "stop_event": state.receiver_state.stop_event})
            await self.send(json.dumps({"ack": "receiver_started"}))

        elif cmd == "stop_receiver":
            state.receiver_state.stop()
            await self.send(json.dumps({"ack": "receiver_stopped"}))

        elif cmd == "ping":
            await self.send(json.dumps({"ack": "pong"}))

        else:
            await self.send(json.dumps({"error": f"unknown command: {cmd}"}))

    # Called when a message is sent to the group via channel_layer.group_send
    async def dashboard_update(self, event: dict) -> None:
        await self.send(json.dumps(event))
