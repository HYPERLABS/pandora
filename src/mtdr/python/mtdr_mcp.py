import click
import re
import grpc
from typing import Any
from fastmcp import FastMCP
from generated.radium_public_pb2_grpc import RadiumStub
import generated.radium_public_pb2 as radium_public_pb2


class MTDRMCPServer:
    def __init__(self):
        self.mcp = FastMCP(name="mtdr-tools", version="0.1.0")
        self._PRESET_ALIASES: dict[str, radium_public_pb2.TDRConfigurationPreset] = {
            "3.2ns/0.2ps": radium_public_pb2.TDR_CONFIGURATION_PRESET_PULSE_PERIOD_3P2_NS_SAMPLE_SPACING_0P2_PS,
            "6.4ns/0.4ps": radium_public_pb2.TDR_CONFIGURATION_PRESET_PULSE_PERIOD_6P4_NS_SAMPLE_SPACING_0P4_PS,
            "12.8ns/0.8ps": radium_public_pb2.TDR_CONFIGURATION_PRESET_PULSE_PERIOD_12P8_NS_SAMPLE_SPACING_0P8_PS,
            "16.0ns/1.0ps": radium_public_pb2.TDR_CONFIGURATION_PRESET_PULSE_PERIOD_16P0_NS_SAMPLE_SPACING_1P0_PS,
            "32.0ns/2.0ps": radium_public_pb2.TDR_CONFIGURATION_PRESET_PULSE_PERIOD_32P0_NS_SAMPLE_SPACING_2P0_PS,
            "64.0ns/4.0ps": radium_public_pb2.TDR_CONFIGURATION_PRESET_PULSE_PERIOD_64P0_NS_SAMPLE_SPACING_4P0_PS,
            "80.0ns/5.0ps": radium_public_pb2.TDR_CONFIGURATION_PRESET_PULSE_PERIOD_80P0_NS_SAMPLE_SPACING_5P0_PS,
            "128.0ns/8.0ps": radium_public_pb2.TDR_CONFIGURATION_PRESET_PULSE_PERIOD_128P0_NS_SAMPLE_SPACING_8P0_PS,
            "160.0ns/10.0ps": radium_public_pb2.TDR_CONFIGURATION_PRESET_PULSE_PERIOD_160P0_NS_SAMPLE_SPACING_10P0_PS,
            "16.0ns/50.0ps": radium_public_pb2.TDR_CONFIGURATION_PRESET_PULSE_PERIOD_16P0_NS_SAMPLE_SPACING_50P0_PS,
            "16.0ns/100.0ps": radium_public_pb2.TDR_CONFIGURATION_PRESET_PULSE_PERIOD_16P0_NS_SAMPLE_SPACING_100P0_PS,
        }

        # Register tools
        self.mcp.tool(self.set_address)
        self.mcp.tool(self.ping)
        self.mcp.tool(self.list_tdr_presets)
        self.mcp.tool(self.configure_tdr_preset)
        self.mcp.tool(self.get_tdr_configuration)
        self.mcp.tool(self.enable_tdr)
        self.mcp.tool(self.get_one_sample_stream)

    def _connect(self) -> RadiumStub:
        channel = grpc.insecure_channel(self.address)
        return RadiumStub(channel)

    def _normalize_preset(self, s: str) -> radium_public_pb2.TDRConfigurationPreset:
        s0 = s.strip().lower()
        if s0 in self._PRESET_ALIASES:
            return self._PRESET_ALIASES[s0]
        nums = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)", s0)]
        if len(nums) >= 2:
            ns, ps = nums[0], nums[1]
            key = f"{ns:.1f}ns/{ps:.1f}ps"
            if key in self._PRESET_ALIASES:
                return self._PRESET_ALIASES[key]
        raise ValueError(
            f"Unrecognized preset '{s}'. Try one of: {', '.join(sorted(self._PRESET_ALIASES.keys()))}"
        )

    def set_address(self, address: str):
        """Set the address of the mtdr we wand to communicate with."""
        self.address = address

    def ping(self) -> str:
        """Sanity check against the any services we need."""
        try:
            stub = self._connect()
            stub.IsReady(radium_public_pb2.IsReadyRequest(), timeout=1.0)
            return "radium: ready"
        except Exception as e:
            return f"radium: error: {type(e).__name__}: {e}"

    def list_tdr_presets(self) -> list[str]:
        """Return friendly strings for valid TDR configuration presets."""
        return sorted(self._PRESET_ALIASES.keys())

    def configure_tdr_preset(self, preset: str) -> dict[str, Any]:
        """Configure the instrument to a named TDR preset (e.g., '16.0ns/1.0ps') and return the applied configuration which was read-back."""
        stub = self._connect()
        enum_val = self._normalize_preset(preset)
        stub.ConfigureTDRPreset(radium_public_pb2.ConfigureTDRPresetRequest(preset=enum_val))
        try:
            cfg = stub.GetTDRConfiguration(radium_public_pb2.GetTDRConfigurationRequest())
            return {
                "pulse_period_enum": int(cfg.pulse_period),
                "pulse_period_ns": float(cfg.pulse_period_ns),
                "sample_spacing_ps": float(cfg.sample_spacing_ps)
            }
        except Exception as e:
            return {
                "pulse_period_enum": "error - unknown",
                "pulse_period_ns": "error - unknown",
                "sample_spacing_ps": "error - unknown"
            }
    
    def get_tdr_configuration(self) -> dict[str, Any]:
        """Read the tdr configuration."""
        stub = self._connect()
        try:
            cfg = stub.GetTDRConfiguration(radium_public_pb2.GetTDRConfigurationRequest())
            return {
                "pulse_period_enum": int(cfg.pulse_period),
                "pulse_period_ns": float(cfg.pulse_period_ns),
                "sample_spacing_ps": float(cfg.sample_spacing_ps)
            }
        except Exception as e:
            return {
                "pulse_period_enum": "error - unknown",
                "pulse_period_ns": "error - unknown",
                "sample_spacing_ps": "error - unknown"
            }

    def enable_tdr(self, enable: bool) -> dict[str, Any]:
        """Enable/disable TDR acquisition. Returns current working state."""
        stub = self._connect()
        stub.EnableTDR(radium_public_pb2.EnableTDRRequest(enable=enable))
        ws = stub.GetTDRWorkingState(radium_public_pb2.GetTDRWorkingStateRequest())
        return {
            "acquisition_enabled": bool(ws.acquisition_enabled),
            "acquiring": bool(ws.acquiring),
            "acquisition_stalled": (ws.acquisition_stalled if ws.HasField("acquisition_stalled") else None),
        }

    def get_one_sample_stream(self, include_samples: bool = False, max_samples: int = 16000, timeout_s: float = 2.0) -> dict[str, Any]:
        """
        Fetch exactly one SampleStream from Radium. Optionally include a capped/decimated
        'samples' array so a client/agent can operate on it (mean, RMS, discontinuity, etc.).

        Args:
            include_samples: Include 'samples' array in the return (may be decimated/truncated).
            max_samples: Maximum samples to return if include_samples=True.
            timeout_s: gRPC deadline for the streaming call.

        Returns:
            {
              "count": int,
              "summary": {"min": float|null, "max": float|null, "mean": float|null},
              "sample_spacing_ps": float,
              "pulse_period_ns": float,
              "ref_50ohm": float,
              "ref_unit_amp": float,
              "samples": [float, ...]   # only if include_samples=True
            }
        """
        stub = self._connect()
        sample_stream_listener_it = stub.ListenToSampleStream(radium_public_pb2.ListenToSampleStreamRequest(), timeout=timeout_s)
        try:
            # Get a sample stream.
            msg = next(sample_stream_listener_it)
            n = len(msg.sample)
            if n == 0:
                return {
                    "count": 0,
                    "summary": {"min": None, "max": None, "mean": None},
                    "sample_spacing_ps": float(msg.sample_spacing_ps),
                    "pulse_period_ns": float(msg.pulse_period_ns),
                    "ref_50ohm": float(msg.ref_50ohm),
                    "ref_unit_amp": float(msg.ref_unit_amp),
                }

            # Compute basic statistics.
            smin = float(min(msg.sample))
            smax = float(max(msg.sample))
            smean = float(sum(msg.sample) / n)
            result: dict[str, Any] = {
                "count": n,
                "summary": {"min": smin, "max": smax, "mean": smean},
                "sample_spacing_ps": float(msg.sample_spacing_ps),
                "pulse_period_ns": float(msg.pulse_period_ns),
                "ref_50ohm": float(msg.ref_50ohm),
                "ref_unit_amp": float(msg.ref_unit_amp),
            }

            # If needed, include the samples and return.
            if include_samples:
                data: list[float] = list(msg.sample)
                result["samples"] = [round(float(x), 1) for x in data]
            return result

        finally:
            try:
                sample_stream_listener_it.cancel()
            except Exception:
                pass

    def run(self):
        self.mcp.run()


# Entry point
@click.command()
@click.option('--address', '-a', help='Address to connect to', default="192.168.1.92:50052")
def cli(address):
    mtdr_mcp_server = MTDRMCPServer()
    mtdr_mcp_server.set_address(address)
    mtdr_mcp_server.run()

if __name__ == "__main__":
    cli()
