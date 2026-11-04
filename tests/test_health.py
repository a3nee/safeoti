"""Testes dos parsers e veredictos de core.health (sem executar comandos)."""

import json
import unittest

from core import health
from core.health import (
    VERDICT_CRITICAL,
    VERDICT_OK,
    VERDICT_UNKNOWN,
    VERDICT_WARNING,
    BatteryInfo,
    DiskHealth,
    evaluate_battery,
    evaluate_disk,
    parse_battery_xml,
    parse_disk_json,
)

NVME_DISK = {
    "DeviceId": "0", "FriendlyName": "Samsung SSD 980 PRO 1TB", "MediaType": "SSD", "BusType": "NVMe",
    "Size": 1000204886016, "HealthStatus": "Healthy", "OperationalStatus": "OK", "SerialNumber": "S5GX.",
    "Reliability": {
        "Temperature": 41, "TemperatureMax": 70, "Wear": 3, "ReadErrorsTotal": 0, "WriteErrorsTotal": 0,
        "PowerOnHours": 5120, "StartStopCycleCount": None,
    },
    "ReliabilityError": "", "ReliabilityDenied": False,
}

HDD_DISK = {
    "DeviceId": "1", "FriendlyName": "WDC WD10EZEX-08WN4A0", "MediaType": "HDD", "BusType": "SATA",
    "Size": 1000204886016, "HealthStatus": "Healthy", "OperationalStatus": "OK", "SerialNumber": "WD-1",
    "Reliability": None, "ReliabilityError": "Access denied", "ReliabilityDenied": True,
}


class DiskParserTests(unittest.TestCase):
    def test_full_object_with_list(self):
        text = json.dumps({"Disks": [HDD_DISK, NVME_DISK], "FailurePredict": [], "FailurePredictDenied": True})
        report = parse_disk_json(text)
        self.assertEqual(report.error, "")
        self.assertEqual([d.device_id for d in report.disks], ["0", "1"])
        nvme, hdd = report.disks
        self.assertEqual(nvme.kind, "NVMe")
        self.assertEqual(nvme.temperature, 41)
        self.assertEqual(nvme.power_on_hours, 5120)
        self.assertIsNone(nvme.start_stop_cycles)
        self.assertEqual(nvme.serial, "S5GX")
        self.assertTrue(nvme.reliability_available)
        self.assertEqual(nvme.verdict, VERDICT_OK)
        self.assertEqual(hdd.kind, "HDD")
        self.assertIsNone(hdd.temperature)
        self.assertTrue(hdd.needs_admin)
        self.assertEqual(hdd.verdict, VERDICT_OK)
        self.assertTrue(report.needs_admin)

    def test_single_object_not_list(self):
        text = json.dumps({"Disks": NVME_DISK, "FailurePredict": None})
        report = parse_disk_json(text)
        self.assertEqual(len(report.disks), 1)
        self.assertFalse(report.needs_admin)

    def test_bare_disk_object_and_numeric_enums(self):
        item = {"DeviceId": 2, "FriendlyName": "Disk", "MediaType": 3, "BusType": 7, "HealthStatus": 1, "Size": "500"}
        report = parse_disk_json(json.dumps(item))
        disk = report.disks[0]
        self.assertEqual(disk.media_type, "HDD")
        self.assertEqual(disk.bus_type, "USB")
        self.assertEqual(disk.health_status, "Warning")
        self.assertEqual(disk.size, 500)
        self.assertEqual(disk.verdict, VERDICT_WARNING)

    def test_missing_fields(self):
        report = parse_disk_json(json.dumps([{"FriendlyName": "X"}]))
        disk = report.disks[0]
        self.assertIsNone(disk.size)
        self.assertEqual(disk.kind, "")
        self.assertEqual(disk.verdict, VERDICT_UNKNOWN)
        self.assertFalse(disk.needs_admin)

    def test_empty_and_invalid(self):
        self.assertTrue(parse_disk_json("").error)
        self.assertTrue(parse_disk_json("not json").error)
        self.assertTrue(parse_disk_json(json.dumps({"Disks": []})).error)

    def test_zero_temperature_is_unknown(self):
        item = dict(NVME_DISK, Reliability=dict(NVME_DISK["Reliability"], Temperature=0, TemperatureMax=0))
        disk = parse_disk_json(json.dumps(item)).disks[0]
        self.assertIsNone(disk.temperature)
        self.assertIsNone(disk.temperature_max)

    def test_failure_predict_single_disk(self):
        text = json.dumps({"Disks": [NVME_DISK], "FailurePredict": {"InstanceName": "X", "PredictFailure": True}})
        disk = parse_disk_json(text).disks[0]
        self.assertTrue(disk.predict_failure)
        self.assertEqual(disk.verdict, VERDICT_CRITICAL)

    def test_failure_predict_matched_by_model(self):
        predict = [
            {"InstanceName": r"SCSI\Disk&Ven_WDC&Prod_WD10EZEX-08WN4A0\4&1&0_0", "PredictFailure": True},
            {"InstanceName": r"SCSI\Disk&Ven_NVMe&Prod_Samsung_SSD_980\5&2&0_0", "PredictFailure": False},
        ]
        report = parse_disk_json(json.dumps({"Disks": [NVME_DISK, HDD_DISK], "FailurePredict": predict}))
        by_id = {d.device_id: d for d in report.disks}
        self.assertTrue(by_id["1"].predict_failure)
        self.assertFalse(by_id["0"].predict_failure)


class DiskVerdictTests(unittest.TestCase):
    def _disk(self, **kwargs):
        base = dict(health_status="Healthy", operational_status="OK", media_type="SSD", bus_type="NVMe")
        base.update(kwargs)
        return evaluate_disk(DiskHealth(**base))

    def test_healthy(self):
        disk = self._disk(temperature=40, wear=10, read_errors=0, write_errors=0)
        self.assertEqual(disk.verdict, VERDICT_OK)
        self.assertEqual(disk.reasons, [])

    def test_unhealthy(self):
        self.assertEqual(self._disk(health_status="Unhealthy").verdict, VERDICT_CRITICAL)

    def test_wear(self):
        self.assertEqual(self._disk(wear=health.DISK_WEAR_WARNING).verdict, VERDICT_OK)
        self.assertEqual(self._disk(wear=health.DISK_WEAR_WARNING + 1).verdict, VERDICT_WARNING)
        self.assertEqual(self._disk(wear=health.DISK_WEAR_CRITICAL).verdict, VERDICT_CRITICAL)

    def test_temperature_thresholds_by_type(self):
        self.assertEqual(self._disk(temperature=58).verdict, VERDICT_OK)
        self.assertEqual(self._disk(temperature=58, media_type="HDD", bus_type="SATA").verdict, VERDICT_WARNING)
        self.assertEqual(self._disk(temperature=61).verdict, VERDICT_WARNING)
        self.assertEqual(self._disk(temperature=health.DISK_TEMP_CRITICAL + 1).verdict, VERDICT_CRITICAL)

    def test_errors(self):
        disk = self._disk(read_errors=2, write_errors=None)
        self.assertEqual(disk.verdict, VERDICT_WARNING)
        self.assertEqual(len(disk.reasons), 1)

    def test_operational_status(self):
        self.assertEqual(self._disk(operational_status="Degraded").verdict, VERDICT_WARNING)


BATTERY_XML = """<?xml version="1.0" encoding="utf-8"?>
<?xml-stylesheet type='text/xsl' href='C:\\battery-stylesheet.xsl'?>
<BatteryReport xmlns="http://schemas.microsoft.com/battery/2012">
  <Batteries>
    <Battery>
      <Id>A32-K55</Id>
      <Manufacturer>ASUS </Manufacturer>
      <SerialNumber> </SerialNumber>
      <Chemistry>LiON</Chemistry>
      <DesignCapacity>70000</DesignCapacity>
      <FullChargeCapacity>49000</FullChargeCapacity>
      <CycleCount>312</CycleCount>
    </Battery>
  </Batteries>
  <History>
    <HistoryEntry LocalStartDate="2026-11-01T00:00:00" DesignCapacity="70000" FullChargeCapacity="50000" />
    <HistoryEntry LocalStartDate="2026-11-02T00:00:00" DesignCapacity="70000" FullChargeCapacity="49000" />
  </History>
</BatteryReport>
"""

NO_BATTERY_XML = """<?xml version="1.0" encoding="utf-8"?>
<BatteryReport xmlns="http://schemas.microsoft.com/battery/2012"><Batteries /></BatteryReport>
"""


class BatteryTests(unittest.TestCase):
    def test_parse(self):
        report = parse_battery_xml(BATTERY_XML)
        self.assertTrue(report.present)
        self.assertEqual(report.error, "")
        bat = report.batteries[0]
        self.assertEqual(bat.id, "A32-K55")
        self.assertEqual(bat.manufacturer, "ASUS")
        self.assertEqual(bat.serial, "")
        self.assertEqual(bat.chemistry, "LiON")
        self.assertEqual(bat.design_capacity, 70000)
        self.assertEqual(bat.full_charge_capacity, 49000)
        self.assertEqual(bat.cycle_count, 312)
        self.assertAlmostEqual(bat.wear_percent, 30.0)
        self.assertAlmostEqual(bat.health_percent, 70.0)
        self.assertEqual(bat.verdict, VERDICT_WARNING)
        self.assertEqual(report.verdict, VERDICT_WARNING)
        self.assertEqual([p.date for p in report.history], ["2026-11-01", "2026-11-02"])
        self.assertEqual(report.history[0].full_charge_capacity, 50000)

    def test_no_battery(self):
        for text in (NO_BATTERY_XML, ""):
            report = parse_battery_xml(text)
            self.assertFalse(report.present)
            self.assertEqual(report.error, "")
            self.assertEqual(report.batteries, [])
            self.assertEqual(report.verdict, VERDICT_UNKNOWN)

    def test_invalid_xml(self):
        report = parse_battery_xml("<BatteryReport")
        self.assertTrue(report.error)

    def test_missing_fields(self):
        xml = '<BatteryReport xmlns="http://schemas.microsoft.com/battery/2012"><Batteries><Battery>' \
              '<Id>X</Id><DesignCapacity>0</DesignCapacity></Battery></Batteries></BatteryReport>'
        bat = parse_battery_xml(xml).batteries[0]
        self.assertIsNone(bat.design_capacity)
        self.assertIsNone(bat.wear_percent)
        self.assertIsNone(bat.cycle_count)
        self.assertEqual(bat.verdict, VERDICT_UNKNOWN)

    def test_verdicts(self):
        def verdict(full, design=100, cycles=None):
            return evaluate_battery(BatteryInfo(design_capacity=design, full_charge_capacity=full,
                                                cycle_count=cycles)).verdict
        self.assertEqual(verdict(100), VERDICT_OK)
        self.assertEqual(verdict(105), VERDICT_OK)  # acima da capacidade de fábrica: desgaste 0
        self.assertEqual(verdict(80), VERDICT_OK)
        self.assertEqual(verdict(79), VERDICT_WARNING)
        self.assertEqual(verdict(59), VERDICT_CRITICAL)
        self.assertEqual(verdict(95, cycles=health.BATTERY_CYCLES_WARNING + 1), VERDICT_WARNING)


if __name__ == "__main__":
    unittest.main()
