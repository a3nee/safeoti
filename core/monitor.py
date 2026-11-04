"""Recolha de métricas do sistema (CPU, RAM, discos, rede e temperaturas)."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import psutil

from .i18n import tr
from .utils import IS_WINDOWS, run_hidden

log = logging.getLogger("safeoti.monitor")


@dataclass
class DiskUsage:
    mountpoint: str
    fstype: str
    total: int
    used: int
    free: int
    percent: float


@dataclass
class Snapshot:
    timestamp: float
    cpu_total: float
    cpu_per_core: List[float]
    cpu_freq_current: Optional[float]  # MHz
    cpu_freq_max: Optional[float]
    ram_total: int
    ram_used: int
    ram_free: int
    ram_percent: float
    disks: List[DiskUsage]
    disk_read_bps: float
    disk_write_bps: float
    disk_rates: Dict[str, tuple] = field(default_factory=dict)  # disco físico -> (leitura, escrita)
    net_down_bps: float = 0.0
    net_up_bps: float = 0.0
    temperatures: Dict[str, float] = field(default_factory=dict)  # etiqueta -> °C


class _TemperatureReader:
    """Lê temperaturas, se o sistema as disponibilizar; caso contrário desiste em silêncio.

    No Windows o psutil não lê sensores; tenta-se a zona térmica ACPI via WMI
    (nem todos os PCs a expõem). A leitura é feita numa thread própria, a cada
    10 segundos, para não atrasar o resto da monitorização.
    """

    INTERVAL = 10.0

    def __init__(self) -> None:
        self._values: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._available = True
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="safeoti-temperaturas", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def values(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._values)

    def _loop(self) -> None:
        while not self._stop.is_set() and self._available:
            try:
                values = self._read()
            except Exception as exc:  # noqa: BLE001 - temperaturas são opcionais
                log.debug("Leitura de temperaturas falhou: %s", exc)
                values = {}
            if not values:
                self._available = False
                log.info("Temperaturas não disponíveis neste sistema; secção escondida.")
            with self._lock:
                self._values = values
            self._stop.wait(self.INTERVAL)

    def _read(self) -> Dict[str, float]:
        values: Dict[str, float] = {}
        sensors = getattr(psutil, "sensors_temperatures", None)
        if sensors is not None:
            for chip, readings in (sensors() or {}).items():
                for index, reading in enumerate(readings):
                    if reading.current and 0 < reading.current < 150:
                        label = reading.label or f"{chip} {index + 1}"
                        values[label] = float(reading.current)
            if values:
                return values
        if IS_WINDOWS:
            result = run_hidden(
                [
                    "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                    "Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature "
                    "-ErrorAction Stop | ForEach-Object { $_.CurrentTemperature }",
                ],
                timeout=15,
            )
            if result.ok:
                for index, line in enumerate(result.stdout.split()):
                    try:
                        celsius = int(line) / 10.0 - 273.15  # décimas de Kelvin
                    except ValueError:
                        continue
                    if 0 < celsius < 150:
                        values[tr("Zona térmica {number}", number=index + 1)] = round(celsius, 1)
        return values


class SystemMonitor:
    """Recolhe amostras do sistema; as taxas são calculadas entre amostras consecutivas."""

    def __init__(self) -> None:
        psutil.cpu_percent(None)
        psutil.cpu_percent(None, percpu=True)
        self._last_time = time.monotonic()
        self._last_disk = self._disk_counters()
        self._last_disk_per = self._disk_counters(perdisk=True)
        self._last_net = self._net_counters()
        self._temps = _TemperatureReader()
        self._temps.start()
        self._partitions_cache: list = []
        self._partitions_time = 0.0

    def close(self) -> None:
        self._temps.stop()

    @staticmethod
    def _disk_counters(perdisk: bool = False):
        try:
            return psutil.disk_io_counters(perdisk=perdisk)
        except (RuntimeError, OSError):
            return None

    @staticmethod
    def _net_counters():
        try:
            return psutil.net_io_counters()
        except (RuntimeError, OSError):
            return None

    def _partitions(self):
        # A lista de partições muda raramente: atualizar a cada 10 s.
        now = time.monotonic()
        if not self._partitions_cache or now - self._partitions_time > 10:
            try:
                parts = psutil.disk_partitions(all=False)
            except OSError:
                parts = []
            self._partitions_cache = [
                p for p in parts if p.fstype and "cdrom" not in (p.opts or "").lower()
            ]
            self._partitions_time = now
        return self._partitions_cache

    def _disks(self) -> List[DiskUsage]:
        disks: List[DiskUsage] = []
        for part in self._partitions():
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except (OSError, PermissionError):
                continue
            disks.append(DiskUsage(part.mountpoint, part.fstype, usage.total, usage.used, usage.free, usage.percent))
        return disks

    def sample(self) -> Snapshot:
        now = time.monotonic()
        elapsed = max(1e-3, now - self._last_time)
        self._last_time = now

        cpu_total = psutil.cpu_percent(None)
        per_core = psutil.cpu_percent(None, percpu=True)
        freq_current = freq_max = None
        try:
            freq = psutil.cpu_freq()
            if freq:
                freq_current = freq.current or None
                freq_max = freq.max or None
        except (OSError, NotImplementedError, AttributeError):
            pass

        mem = psutil.virtual_memory()

        read_bps = write_bps = 0.0
        disk = self._disk_counters()
        if disk and self._last_disk:
            read_bps = max(0, disk.read_bytes - self._last_disk.read_bytes) / elapsed
            write_bps = max(0, disk.write_bytes - self._last_disk.write_bytes) / elapsed
        self._last_disk = disk

        disk_rates: Dict[str, tuple] = {}
        per = self._disk_counters(perdisk=True) or {}
        last_per = self._last_disk_per or {}
        for name, counters in per.items():
            previous = last_per.get(name)
            if previous:
                disk_rates[name] = (
                    max(0, counters.read_bytes - previous.read_bytes) / elapsed,
                    max(0, counters.write_bytes - previous.write_bytes) / elapsed,
                )
        self._last_disk_per = per

        down = up = 0.0
        net = self._net_counters()
        if net and self._last_net:
            down = max(0, net.bytes_recv - self._last_net.bytes_recv) / elapsed
            up = max(0, net.bytes_sent - self._last_net.bytes_sent) / elapsed
        self._last_net = net

        return Snapshot(
            timestamp=time.time(),
            cpu_total=cpu_total,
            cpu_per_core=list(per_core),
            cpu_freq_current=freq_current,
            cpu_freq_max=freq_max,
            ram_total=mem.total,
            ram_used=mem.total - mem.available,
            ram_free=mem.available,
            ram_percent=mem.percent,
            disks=self._disks(),
            disk_read_bps=read_bps,
            disk_write_bps=write_bps,
            disk_rates=disk_rates,
            net_down_bps=down,
            net_up_bps=up,
            temperatures=self._temps.values,
        )
