"""Saúde do hardware: discos (SMART/fiabilidade) e bateria.

Só leitura. Usa mecanismos documentados do Windows:

* Discos: ``Get-PhysicalDisk`` + ``Get-StorageReliabilityCounter`` (PowerShell)
  e, como complemento, ``MSStorageDriver_FailurePredictStatus`` (WMI root\\wmi).
  Os contadores de fiabilidade e a previsão de falha normalmente exigem
  privilégios de administrador; sem eles os campos ficam ``None`` e o
  resultado indica ``needs_admin``.
* Bateria: ``powercfg /batteryreport /xml`` (XML independente do idioma).

A execução dos comandos está separada da interpretação (``parse_*``) para que
os parsers possam ser testados sem Windows.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .i18n import tr
from .utils import IS_WINDOWS, run_hidden

log = logging.getLogger("safeoti.health")

# --------------------------------------------------------------------------- #
# Veredictos e limites
# --------------------------------------------------------------------------- #
VERDICT_OK = "ok"
VERDICT_WARNING = "atencao"
VERDICT_CRITICAL = "critico"
VERDICT_UNKNOWN = "desconhecido"

_SEVERITY = {VERDICT_UNKNOWN: 0, VERDICT_OK: 1, VERDICT_WARNING: 2, VERDICT_CRITICAL: 3}

# Discos
DISK_WEAR_WARNING = 80          # % de desgaste (SSD) acima do qual se pede atenção
DISK_WEAR_CRITICAL = 95         # % de desgaste considerado crítico
DISK_TEMP_WARNING = 60          # °C (SSD/NVMe)
DISK_TEMP_WARNING_HDD = 55      # °C (discos mecânicos)
DISK_TEMP_CRITICAL = 70         # °C (SSD/NVMe)
DISK_TEMP_CRITICAL_HDD = 65     # °C (discos mecânicos)

# Bateria
BATTERY_WEAR_WARNING = 20       # % de capacidade perdida face à de fábrica
BATTERY_WEAR_CRITICAL = 40
BATTERY_CYCLES_WARNING = 1000   # ciclos de carga

POWERSHELL_TIMEOUT = 45
POWERCFG_TIMEOUT = 60
BATTERY_HISTORY_POINTS = 10

BATTERY_NS = "http://schemas.microsoft.com/battery/2012"

# Códigos numéricos (MSFT_PhysicalDisk) caso o PowerShell devolva enums como números.
_MEDIA_TYPES = {0: "Unspecified", 3: "HDD", 4: "SSD", 5: "SCM"}
_BUS_TYPES = {
    0: "Unknown", 1: "SCSI", 2: "ATAPI", 3: "ATA", 4: "1394", 5: "SSA", 6: "Fibre Channel",
    7: "USB", 8: "RAID", 9: "iSCSI", 10: "SAS", 11: "SATA", 12: "SD", 13: "MMC",
    14: "Virtual", 15: "File Backed Virtual", 16: "Storage Spaces", 17: "NVMe",
}
_HEALTH_STATUS = {0: "Healthy", 1: "Warning", 2: "Unhealthy", 5: "Unknown"}


def worst(*verdicts: str) -> str:
    """Devolve o veredicto mais grave."""
    result = VERDICT_UNKNOWN
    for verdict in verdicts:
        if _SEVERITY.get(verdict, 0) > _SEVERITY[result]:
            result = verdict
    return result


# --------------------------------------------------------------------------- #
# Discos
# --------------------------------------------------------------------------- #
@dataclass
class DiskHealth:
    device_id: str = ""
    name: str = ""
    media_type: str = ""          # "SSD", "HDD", "SCM", "Unspecified"
    bus_type: str = ""            # "NVMe", "SATA", "USB", ...
    size: Optional[int] = None    # bytes
    health_status: str = ""       # "Healthy", "Warning", "Unhealthy", "Unknown"
    operational_status: str = ""
    serial: str = ""
    temperature: Optional[int] = None
    temperature_max: Optional[int] = None
    wear: Optional[int] = None    # %
    read_errors: Optional[int] = None
    write_errors: Optional[int] = None
    power_on_hours: Optional[int] = None
    start_stop_cycles: Optional[int] = None
    reliability_available: bool = False
    needs_admin: bool = False
    predict_failure: Optional[bool] = None
    verdict: str = VERDICT_UNKNOWN
    reasons: List[str] = field(default_factory=list)

    @property
    def kind(self) -> str:
        """Tipo resumido: "NVMe", "SSD", "HDD" ou "" (desconhecido)."""
        if self.bus_type.lower() == "nvme":
            return "NVMe"
        media = self.media_type.upper()
        if media in ("SSD", "HDD", "SCM"):
            return media
        return ""

    @property
    def is_hdd(self) -> bool:
        return self.media_type.upper() == "HDD"


@dataclass
class DiskReport:
    disks: List[DiskHealth] = field(default_factory=list)
    needs_admin: bool = False       # algum contador ficou por ler por falta de privilégios
    error: str = ""                 # falha geral (PowerShell em falta, tempo esgotado, ...)


_DISK_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$disks = @()
foreach ($d in @(Get-PhysicalDisk)) {
    $rel = $null; $relErr = ''; $denied = $false
    try {
        $r = $d | Get-StorageReliabilityCounter -ErrorAction Stop
        if ($r) {
            $rel = [pscustomobject]@{
                Temperature = $r.Temperature; TemperatureMax = $r.TemperatureMax; Wear = $r.Wear
                ReadErrorsTotal = $r.ReadErrorsTotal; WriteErrorsTotal = $r.WriteErrorsTotal
                PowerOnHours = $r.PowerOnHours; StartStopCycleCount = $r.StartStopCycleCount
            }
        }
    } catch {
        $relErr = $_.Exception.Message
        if ($_.CategoryInfo.Category -eq 'PermissionDenied' -or $_.Exception -is [System.UnauthorizedAccessException]) { $denied = $true }
    }
    $disks += [pscustomobject]@{
        DeviceId = [string]$d.DeviceId; FriendlyName = [string]$d.FriendlyName
        MediaType = [string]$d.MediaType; BusType = [string]$d.BusType; Size = $d.Size
        HealthStatus = [string]$d.HealthStatus; OperationalStatus = [string]($d.OperationalStatus -join ', ')
        SerialNumber = [string]$d.SerialNumber
        Reliability = $rel; ReliabilityError = $relErr; ReliabilityDenied = $denied
    }
}
$pf = @(); $pfDenied = $false
try {
    $pf = @(Get-CimInstance -Namespace root\wmi -ClassName MSStorageDriver_FailurePredictStatus -ErrorAction Stop |
        ForEach-Object { [pscustomobject]@{ InstanceName = [string]$_.InstanceName; PredictFailure = [bool]$_.PredictFailure; Reason = $_.Reason } })
} catch {
    if ($_.CategoryInfo.Category -eq 'PermissionDenied') { $pfDenied = $true }
}
[pscustomobject]@{ Disks = @($disks); FailurePredict = @($pf); FailurePredictDenied = $pfDenied } | ConvertTo-Json -Compress -Depth 5
"""


def _as_list(value: Any) -> List[Any]:
    """ConvertTo-Json devolve um objeto (dict) quando só há um elemento."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _to_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None


def _enum_text(value: Any, mapping: Dict[int, str]) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return mapping.get(int(value), str(value))
    text = str(value).strip()
    if text.isdigit():
        return mapping.get(int(text), text)
    return text


def _normalize(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def _match_failure_predict(disk: DiskHealth, entries: List[dict], disk_count: int) -> Optional[bool]:
    """Associa as entradas WMI de previsão de falha a um disco (pelo modelo)."""
    if not entries:
        return None
    if disk_count == 1 and len(entries) == 1:
        return bool(entries[0].get("PredictFailure"))
    key = _normalize(disk.name)[:12]
    if not key:
        return None
    for entry in entries:
        # Ex.: SCSI\Disk&Ven_WDC&Prod_WD10EZEX-08WN4A0\4&...  ->  ...WDCWD10EZEX08WN4A0...
        instance = re.sub(r"(?i)(ven|prod)_", "", str(entry.get("InstanceName", "")))
        if key in _normalize(instance):
            return bool(entry.get("PredictFailure"))
    return None


def parse_disk_record(item: dict) -> DiskHealth:
    """Converte um registo (dict) do script PowerShell num ``DiskHealth``."""
    disk = DiskHealth(
        device_id=str(item.get("DeviceId") or "").strip(),
        name=str(item.get("FriendlyName") or "").strip(),
        media_type=_enum_text(item.get("MediaType"), _MEDIA_TYPES),
        bus_type=_enum_text(item.get("BusType"), _BUS_TYPES),
        size=_to_int(item.get("Size")),
        health_status=_enum_text(item.get("HealthStatus"), _HEALTH_STATUS),
        operational_status=str(item.get("OperationalStatus") or "").strip(),
        serial=str(item.get("SerialNumber") or "").strip().rstrip("."),
    )
    rel = item.get("Reliability")
    if isinstance(rel, list):
        rel = rel[0] if rel else None
    if isinstance(rel, dict):
        disk.reliability_available = True
        disk.temperature = _to_int(rel.get("Temperature"))
        disk.temperature_max = _to_int(rel.get("TemperatureMax"))
        disk.wear = _to_int(rel.get("Wear"))
        disk.read_errors = _to_int(rel.get("ReadErrorsTotal"))
        disk.write_errors = _to_int(rel.get("WriteErrorsTotal"))
        disk.power_on_hours = _to_int(rel.get("PowerOnHours"))
        disk.start_stop_cycles = _to_int(rel.get("StartStopCycleCount"))
        # Muitos controladores devolvem 0 °C quando não reportam temperatura.
        if disk.temperature == 0:
            disk.temperature = None
        if disk.temperature_max == 0:
            disk.temperature_max = None
    else:
        disk.needs_admin = bool(item.get("ReliabilityDenied"))
    return disk


def evaluate_disk(disk: DiskHealth) -> DiskHealth:
    """Calcula o veredicto (ok / atenção / crítico) e as razões."""
    verdict = VERDICT_UNKNOWN
    reasons: List[str] = []

    status = disk.health_status.strip()
    if status.lower() == "healthy":
        verdict = worst(verdict, VERDICT_OK)
    elif status.lower() == "unhealthy":
        verdict = worst(verdict, VERDICT_CRITICAL)
        reasons.append(tr("O Windows indica que o disco não está saudável."))
    elif status.lower() == "warning":
        verdict = worst(verdict, VERDICT_WARNING)
        reasons.append(tr("O Windows reporta um aviso para este disco."))
    elif status and status.lower() != "unknown":
        verdict = worst(verdict, VERDICT_WARNING)
        reasons.append(tr("Estado de saúde reportado: {status}", status=status))

    op = disk.operational_status.strip()
    if op and op.upper() != "OK" and op.lower() != "online":
        verdict = worst(verdict, VERDICT_WARNING)
        reasons.append(tr("Estado operacional: {status}", status=op))

    if disk.predict_failure:
        verdict = worst(verdict, VERDICT_CRITICAL)
        reasons.append(tr("O SMART prevê uma falha do disco. Faça cópia de segurança dos dados."))

    if disk.wear is not None:
        if disk.wear >= DISK_WEAR_CRITICAL:
            verdict = worst(verdict, VERDICT_CRITICAL)
            reasons.append(tr("Desgaste muito elevado ({wear}%).", wear=disk.wear))
        elif disk.wear > DISK_WEAR_WARNING:
            verdict = worst(verdict, VERDICT_WARNING)
            reasons.append(tr("Desgaste elevado ({wear}%).", wear=disk.wear))

    if disk.temperature is not None:
        warn = DISK_TEMP_WARNING_HDD if disk.is_hdd else DISK_TEMP_WARNING
        crit = DISK_TEMP_CRITICAL_HDD if disk.is_hdd else DISK_TEMP_CRITICAL
        if disk.temperature > crit:
            verdict = worst(verdict, VERDICT_CRITICAL)
            reasons.append(tr("Temperatura muito elevada ({temp} °C).", temp=disk.temperature))
        elif disk.temperature > warn:
            verdict = worst(verdict, VERDICT_WARNING)
            reasons.append(tr("Temperatura elevada ({temp} °C).", temp=disk.temperature))

    errors = (disk.read_errors or 0) + (disk.write_errors or 0)
    if errors > 0:
        verdict = worst(verdict, VERDICT_WARNING)
        reasons.append(tr("Erros de leitura/escrita registados: {count}", count=errors))

    disk.verdict = verdict
    disk.reasons = reasons
    return disk


def parse_disk_json(text: str) -> DiskReport:
    """Interpreta o JSON do script de discos.

    Aceita o objeto completo ``{"Disks": ..., "FailurePredict": ...}`` ou,
    por conveniência, só a lista (ou um único objeto) de discos.
    """
    text = (text or "").strip()
    if not text:
        return DiskReport(error=tr("Não foram encontrados discos."))
    try:
        data = json.loads(text)
    except ValueError as exc:
        log.warning("JSON de discos inválido: %s", exc)
        return DiskReport(error=tr("Resposta inválida do Windows ao consultar os discos."))

    if isinstance(data, dict) and "Disks" in data:
        records = _as_list(data.get("Disks"))
        predict = [e for e in _as_list(data.get("FailurePredict")) if isinstance(e, dict)]
        predict_denied = bool(data.get("FailurePredictDenied"))
    else:
        records = _as_list(data)
        predict, predict_denied = [], False

    report = DiskReport()
    records = [r for r in records if isinstance(r, dict)]
    for item in records:
        disk = parse_disk_record(item)
        disk.predict_failure = _match_failure_predict(disk, predict, len(records))
        evaluate_disk(disk)
        report.disks.append(disk)
    report.disks.sort(key=lambda d: (_to_int(d.device_id) is None, _to_int(d.device_id) or 0, d.device_id))
    report.needs_admin = any(d.needs_admin for d in report.disks) or (predict_denied and not predict and bool(records))
    if not report.disks:
        report.error = tr("Não foram encontrados discos.")
    return report


def get_disk_health() -> DiskReport:
    """Consulta os discos físicos (nunca lança exceção)."""
    if not IS_WINDOWS:
        return DiskReport(error=tr("Disponível apenas no Windows."))
    result = run_hidden(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _DISK_SCRIPT],
        timeout=POWERSHELL_TIMEOUT,
        encoding="utf-8",
    )
    if result.returncode < 0:
        return DiskReport(error=result.output)
    if not result.ok and not result.stdout.strip():
        log.warning("Consulta de discos falhou: %s", result.output)
        return DiskReport(error=tr("Não foi possível consultar os discos: {erro}", erro=result.output[:300]))
    return parse_disk_json(result.stdout)


# --------------------------------------------------------------------------- #
# Bateria
# --------------------------------------------------------------------------- #
@dataclass
class CapacityPoint:
    date: str                       # AAAA-MM-DD (local)
    design_capacity: Optional[int]  # mWh
    full_charge_capacity: Optional[int]


@dataclass
class BatteryInfo:
    id: str = ""
    manufacturer: str = ""
    chemistry: str = ""
    serial: str = ""
    design_capacity: Optional[int] = None       # mWh
    full_charge_capacity: Optional[int] = None  # mWh
    cycle_count: Optional[int] = None
    verdict: str = VERDICT_UNKNOWN
    reasons: List[str] = field(default_factory=list)

    @property
    def wear_percent(self) -> Optional[float]:
        """Capacidade perdida face à de fábrica (0–100), ``None`` se desconhecida."""
        if not self.design_capacity or self.full_charge_capacity is None:
            return None
        wear = (1 - self.full_charge_capacity / self.design_capacity) * 100
        return max(0.0, min(100.0, wear))

    @property
    def health_percent(self) -> Optional[float]:
        wear = self.wear_percent
        return None if wear is None else 100.0 - wear


@dataclass
class BatteryReport:
    present: bool = False
    batteries: List[BatteryInfo] = field(default_factory=list)
    history: List[CapacityPoint] = field(default_factory=list)
    error: str = ""

    @property
    def verdict(self) -> str:
        return worst(*(b.verdict for b in self.batteries)) if self.batteries else VERDICT_UNKNOWN


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(elem: ET.Element, name: str) -> str:
    for child in elem:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _find_all(root: ET.Element, name: str) -> List[ET.Element]:
    return [e for e in root.iter() if _local(e.tag) == name]


def evaluate_battery(battery: BatteryInfo) -> BatteryInfo:
    verdict = VERDICT_UNKNOWN
    reasons: List[str] = []
    wear = battery.wear_percent
    if wear is not None:
        verdict = VERDICT_OK
        if wear > BATTERY_WEAR_CRITICAL:
            verdict = VERDICT_CRITICAL
            reasons.append(tr("A bateria perdeu {wear}% da capacidade original. Considere substituí-la.",
                              wear=round(wear)))
        elif wear > BATTERY_WEAR_WARNING:
            verdict = VERDICT_WARNING
            reasons.append(tr("A bateria perdeu {wear}% da capacidade original.", wear=round(wear)))
    if battery.cycle_count is not None and battery.cycle_count > BATTERY_CYCLES_WARNING:
        verdict = worst(verdict, VERDICT_WARNING)
        reasons.append(tr("Número elevado de ciclos de carga ({count}).", count=battery.cycle_count))
    battery.verdict = verdict
    battery.reasons = reasons
    return battery


def parse_battery_xml(text: str) -> BatteryReport:
    """Interpreta o XML de ``powercfg /batteryreport /xml``.

    Sem elementos ``<Battery>`` devolve ``present=False`` (não é erro).
    """
    text = (text or "").strip()
    if not text:
        return BatteryReport(present=False)
    # O ElementTree recusa texto com declaração de codificação; trabalha em bytes.
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError as exc:
        log.warning("XML de bateria inválido: %s", exc)
        return BatteryReport(error=tr("Relatório de bateria inválido."))

    report = BatteryReport()
    for elem in _find_all(root, "Battery"):
        battery = BatteryInfo(
            id=_child_text(elem, "Id"),
            manufacturer=_child_text(elem, "Manufacturer"),
            chemistry=_child_text(elem, "Chemistry"),
            serial=_child_text(elem, "SerialNumber"),
            design_capacity=_to_int(_child_text(elem, "DesignCapacity")) or None,
            full_charge_capacity=_to_int(_child_text(elem, "FullChargeCapacity")),
            cycle_count=_to_int(_child_text(elem, "CycleCount")),
        )
        report.batteries.append(evaluate_battery(battery))
    report.present = bool(report.batteries)

    points: List[CapacityPoint] = []
    for entry in _find_all(root, "HistoryEntry"):
        date = entry.get("LocalStartDate") or entry.get("StartDate") or ""
        points.append(CapacityPoint(
            date=date[:10],
            design_capacity=_to_int(entry.get("DesignCapacity")),
            full_charge_capacity=_to_int(entry.get("FullChargeCapacity")),
        ))
    report.history = points[-BATTERY_HISTORY_POINTS:]
    return report


def has_battery() -> Optional[bool]:
    """``True``/``False`` se for possível determinar; ``None`` se não se souber."""
    try:
        import psutil
        return psutil.sensors_battery() is not None
    except Exception:  # noqa: BLE001 - sensores podem falhar em hardware exótico
        return None


def _battery_report_path(suffix: str) -> str:
    folder = os.path.join(tempfile.gettempdir(), "SafeOti")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"battery-report{suffix}")


def get_battery_health() -> BatteryReport:
    """Lê o estado da bateria (nunca lança exceção)."""
    if not IS_WINDOWS:
        return BatteryReport(present=False)
    if has_battery() is False:
        return BatteryReport(present=False)
    try:
        fd, path = tempfile.mkstemp(prefix="safeoti-battery-", suffix=".xml")
        os.close(fd)
    except OSError as exc:
        return BatteryReport(error=str(exc))
    try:
        result = run_hidden(["powercfg.exe", "/batteryreport", "/xml", "/output", path], timeout=POWERCFG_TIMEOUT)
        if not result.ok:
            log.warning("powercfg /batteryreport falhou: %s", result.output)
            return BatteryReport(error=tr("Não foi possível obter o relatório da bateria: {erro}",
                                          erro=result.output[:300]))
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            return BatteryReport(error=str(exc))
        return parse_battery_xml(data.decode("utf-8", errors="replace"))
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


@dataclass
class HtmlReportResult:
    ok: bool
    path: str = ""
    message: str = ""


def generate_battery_html_report(path: Optional[str] = None) -> HtmlReportResult:
    """Gera o relatório HTML completo do Windows (``powercfg /batteryreport``).

    Sem ``path`` grava em ``%TEMP%\\SafeOti\\battery-report.html``.
    """
    if not IS_WINDOWS:
        return HtmlReportResult(False, message=tr("Disponível apenas no Windows."))
    try:
        target = os.path.abspath(path) if path else _battery_report_path(".html")
        os.makedirs(os.path.dirname(target), exist_ok=True)
    except OSError as exc:
        return HtmlReportResult(False, message=str(exc))
    result = run_hidden(["powercfg.exe", "/batteryreport", "/output", target], timeout=POWERCFG_TIMEOUT)
    if not result.ok or not os.path.isfile(target):
        return HtmlReportResult(False, message=tr("Não foi possível gerar o relatório da bateria: {erro}",
                                                  erro=result.output[:300]))
    return HtmlReportResult(True, path=target)


@dataclass
class HealthSnapshot:
    disks: DiskReport
    battery: BatteryReport


def collect() -> HealthSnapshot:
    """Recolhe discos e bateria (para correr numa thread de trabalho)."""
    return HealthSnapshot(disks=get_disk_health(), battery=get_battery_health())
