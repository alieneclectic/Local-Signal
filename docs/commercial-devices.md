# Commercial device support

Research checked November 8, 2026. The first implementation targets **BACnet/IP** because published evidence supports broad commercial-building adoption. This is a protocol-based starting point, not a claim that a particular controller is the best-selling device.

## Research and priorities

| Priority | Interface | Evidence and use | Status |
| --- | --- | --- | --- |
| 1 | BACnet/IP | BACnet International's summary of BSRIA's 2023 *Market Penetration of Communications Protocols 2018–2027* study reports BACnet specified in 77% of projects worldwide. Its 2025 journal repeats that finding. This is published project/specification adoption evidence, not a fresh 2026 device shipment count. Building HVAC controllers, environmental sensors, and equipment states are the first target. | Experimental direct-unicast reader implemented. |
| 2 | Modbus TCP | The Modbus Organization documents read functions for input/holding registers and industrial monitoring. Useful for meters, drives, and plant equipment. Register maps, word order, scale factors, and identity vary by model, so a protocol port alone is insufficient evidence of support. No comparable current adoption percentage was established by this research. | Experimental configurable read-only reader implemented. |

BACnet International's adoption report identifies Siemens, Automated Logic, Schneider Electric, Honeywell, and Johnson Controls as major manufacturers basing product ranges on BACnet. It does not rank those brands by device sales. Manufacturer documentation independently confirms BACnet/IP in Siemens PXC, Johnson Controls Metasys equipment controllers, and Honeywell IP PBC controllers. These are representative hardware-validation candidates; **none has been tested with Local Signal on physical hardware**. Protocol support on a product page does not guarantee the right configuration, firmware, point access, transport mode, or network reachability for this reader.

Sources:

- [BACnet International adoption report / BSRIA study summary (2023)](https://bacnetinternational.org/wp-content/uploads/sites/2/2023/10/BACnet-Adoption-Market-Research-Report-.pdf)
- [BACnet International Journal, 2025, issue 27](https://bacnetinternational.org/wp-content/uploads/sites/2/2025/04/250429_BJ_OfBuildingAutomation_27_digital.pdf)
- [Siemens PXC Modular Series data sheet](https://sid.siemens.com/api/khub/documents/zz4S336yTm~w2NVS~GAxCA/content)
- [Johnson Controls: Metasys BACnet/IP integration](https://docs.johnsoncontrols.com/bas/r/Metasys/en-US/Metasys-System-Configuration-Guide/16.0/Integrations/BACnet/IP-integration?contentId=dMAapKJSUsY3Olc54Cokfw)
- [Honeywell IP PBC](https://buildings.honeywell.com/us/en/products/by-category/control-panels/building-controls/stand-alone-controllers/ip-pbc)
- [Modbus Organization: specifications](https://www.modbus.org/modbus-specifications) and [introduction, function codes, and register maps](https://www.modbus.org/introduction-to-modbus)

## BACnet/IP setup and supported readings

1. Obtain the device's direct private IPv4 address, BACnet device instance, UDP port, and approved point list from the building administrator. A point list must identify object types and instance numbers. Do not guess point numbers from another model.
2. Select **Add device**, or open an existing device's details. Choose **BACnet/IP — commercial (experimental)** under **Monitoring**.
3. Enter the device instance and port. Add up to 12 points, each with a type, instance, and descriptive label. Labels are local dashboard data. Save; the collector reads the configuration on its next cycle.
4. Open the device to see valid readings and individual unavailable-point reasons. Use **History** to select a device-specific BACnet point, or export its samples as CSV.

Supported types: analog input/output/value, binary input/output/value, and multi-state input/output/value. Even output objects are only read. Analog readings retain the device's engineering unit; unknown units are shown as `BACnet unit N`. Binary states appear as Yes/No and multi-state points as state numbers. No model-specific state text is inferred. Model and installed firmware revision are optional metadata, not update availability evidence.

The client first reads the configured Device object's `object-identifier` (75), then each point's `status-flags` (111), analog `units` (117), and `present-value` (85). It optionally reads Device `model-name` (70) and `firmware-revision` (44). Only confirmed `ReadProperty` service 12 is sent. Packet structure and enum references: [BACnet Stack ReadProperty](https://github.com/bacnet-stack/bacnet-stack/blob/master/src/bacnet/rp.c), [enumerations](https://github.com/bacnet-stack/bacnet-stack/blob/master/src/bacnet/bacenum.h), and [NPDU](https://github.com/bacnet-stack/bacnet-stack/blob/master/src/bacnet/npdu.c).

## BACnet/IP boundaries and data semantics

- This reader supports directly addressed BACnet/IP devices replying with small, unsegmented Original-Unicast-NPDU packets. It uses an ephemeral local UDP port; the device and network must permit replies to that port. No broadcast discovery, BBMD registration, routed MS/TP destinations, COV subscriptions, BACnet/SC, or serial transport is included. A gateway is only usable if it exposes the configured objects itself as a directly addressed BACnet/IP device.
- Configuration is opt-in per inventory device. Automatic scans and ordinary devices keep their existing behavior. There is one configured BACnet device per inventory IP address; multiple logical devices behind one IP are not supported.
- Each device poll has a 10-second budget, 750-ms request timeout, and no retries. Twelve analog points require at most 39 small requests, including identity and optional metadata. Slow or unavailable devices can leave later points unread. The dashboard reports those points as unavailable.
- Faulted, overridden, out-of-service, malformed, nonfinite, unsupported, and missing values are omitted, never replaced by zero. A healthy observed zero is retained. One valid point makes a partial device read reporting; point failures remain visible. A complete failure retains earlier history separately from current readings.
- Device instance numbers verify the configured endpoint but are not treated as globally unique hardware identities. BACnet data does not manufacture a MAC address. Existing hardware identity and IP-move rules remain in effect when a MAC has been independently established.
- Point keys include the configured device instance and object identifier; analog keys also include the unit code. This avoids combining unrelated controller points or silently changing historical units. Labels can be edited without changing point identity. Points are not automatically classified as room temperature, aggregate power, or heating-energy meters.
- Monitoring configuration changes clear displayed telemetry and reject results already in flight from the previous configuration. Labels, notes, settings, and stored history survive migration and reconfiguration. Returning Monitoring to **Automatic local API** removes the BACnet configuration.
- Only private IPv4 destinations are accepted. Requests use direct UDP sockets with bounded replies; HTTP proxies, redirects, cloud services, and vendor calls are not involved. This integration does not add dashboard authentication or change the existing LAN access model.

## Modbus TCP setup and supported readings

1. Obtain the exact model's register map, private IPv4 address, TCP port, and unit ID from the equipment administrator. The default port is 502; the specification recommends unit 255 for direct TCP. Unit 0 is also accepted for direct TCP; gateways normally use the target's unit ID from 1–247. Use the value configured for your equipment.
2. Select **Add device** or open an existing device's details, then choose **Modbus TCP — commercial (experimental)** under **Monitoring**.
3. Configure up to 12 points. Choose **Holding registers (03)**, **Input registers (04)**, **Coils (01)**, or **Discrete inputs (02)**. Every point needs a zero-based address and a local label. There is one configured unit ID per inventory IP address; multiple downstream units at one gateway IP cannot be added as separate inventory devices in this version.
4. For registers, select `uint16`, `int16`, `uint32`, `int32`, `float32`, or `float64`. These read 1, 1, 2, 2, 2, or 4 adjacent registers respectively in a single request. Choose byte order within each 16-bit register and high/low word order across registers according to the manual. Signed types use two's complement; floating types use IEEE 754. Integer 64-bit, strings, packed register bitfields, and vendor-specific types are not supported.
5. Enter units, scale, and offset from the map. The displayed value is `decoded raw × scale + offset`. Scale must be nonzero and all conversions finite. If the vendor defines a finite raw sentinel for unavailable readings, enter its decoded numeric value under **Raw invalid value**; it is checked before scaling. For example, signed int16 bits `0xffff` decode to −1. The marker must fit the selected numeric type; float32 markers use that format's rounding. Coils and discrete inputs are single-bit Yes/No readings with no numeric conversion.
6. Save. The collector uses the map on its next cycle. Open device details for point failures; select the device-specific point under **History**, or export samples as CSV.

Addresses are the protocol's **zero-based offsets**, always 0–65535 with enough space for the full value. For example, where a manual uses the conventional holding-register reference `40001`, select Holding registers and enter `0`; `30001` conventionally means Input registers offset `0`. Some manuals already give offsets, and others use different reference ranges. Confirm the convention rather than subtracting a fixed number from every vendor address.

Synthetic examples, not a supported model map:

| Point | Table / address | Format | Conversion / unit | Example result |
| --- | --- | --- | --- | --- |
| Active power | Holding / 0 | float32, high byte and word first | × 0.001, kW | Raw 1234.5 → 1.2345 kW |
| Supply air | Input / 10 | int16 | × 0.1, °C | Raw −125 → −12.5 °C |
| Fan running | Coil / 2 | bit | Yes/No | Observed 0 → No |

Protocol references: [Modbus Application Protocol Specification V1.1b3](https://www.modbus.org/file/secure/modbusprotocolspecification.pdf) (functions, addressing, encoding, exceptions) and [Modbus TCP Messaging Implementation Guide V1.0b](https://www.modbus.org/file/secure/messagingimplementationguide.pdf) (MBAP framing, transactions, and unit IDs). Multi-register numeric layout, engineering units, scaling, and invalid markers remain model-specific.

## Modbus TCP boundaries and data semantics

- Only TCP read functions 01–04 are available; no caller-provided function code or write payload is accepted. Collection does not scan Modbus ports or guess maps. This is plain Modbus TCP, not TLS/Modbus Security, UDP, RTU-over-TCP, or direct serial access. A TCP-to-serial gateway can be used only when it presents standard Modbus TCP for the single configured unit.
- Each poll uses one direct connection to a validated private IPv4 address, up to 12 small requests, no retries, a 750-ms connection/request timeout, and a 10-second total budget. The request deadline is absolute across all fragmented header/body reads, so trickling bytes cannot extend a request indefinitely. Responses must match the transaction, protocol, unit ID, function, and expected size. The unit ID must be echoed, including for direct TCP. No proxy, redirect, cloud, or vendor lookup is involved.
- A valid Modbus exception makes that point unavailable while allowing subsequent reads. A transport, deadline, or framing failure stops requests on that connection and marks later points unread. Earlier valid readings may still make the device partially reporting. Failed readings are omitted rather than replaced with zero. Observed zero is retained; earlier readings remain historical on total failure.
- Modbus does not provide universal quality flags for these tables. A valid finite response is interpreted using the configured map; the reader cannot detect a wrong but plausible map or vendor fault codes unless their raw sentinel is configured. NaN, infinity, scaled overflow, and configured invalid raw values are never stored as measurements.
- A unit ID, TCP address, or successful read is not a verified hardware identity. No MAC, model, firmware revision, or firmware-update evidence is invented. Previously established inventory MAC and IP-move behavior continues to apply, but a gateway MAC does not identify a downstream meter. Register values are not automatically classified as room temperature, aggregate power, or heating-energy counters.
- History keys include inventory identity, unit ID, table, zero-based address, and interpretation settings (including port, type, byte/word order, units, scale, offset, and invalid marker). This avoids joining unrelated devices or changing the meaning of old samples. Label edits keep the series. Mapping changes clear displayed telemetry and reject prior in-flight results while retaining labels, notes, settings, and history. The existing `monitor_config` column is reused; no additional schema migration is required.

## Hardware validation

For repeatable automated collection/storage/API checks and an interactive dashboard connected to actual simulator/public-demo packets, use the [commercial end-to-end test guide](commercial-e2e.md). Public Pult access is opt-in and confined to test-process socket transports; normal app validation still rejects public addresses.

The automated suite uses synthetic packets, a loopback UDP responder, and temporary SQLite databases. An independent development check also passed against **BACpypes3 0.0.110** on loopback, installed only in a temporary environment: standard Analog Input (22.5 °C), Binary Input (inactive/zero), and Multi-state Value (state 2) objects returned the expected values and units through this reader. Neither establishes physical-device compatibility. BACpypes3 is not a runtime or test-suite dependency.

The Modbus suite uses an actual loopback TCP responder, fragmented replies, and temporary databases. A separate check passed against **PyModbus 3.16.2** on loopback, installed only in a temporary environment: holding-register float32 raw 1234.5 scaled to 1.2345 kW, input-register int16 raw −125 scaled to −12.5 °C, a zero coil, and an active discrete input all returned the expected values. PyModbus is not a runtime or test-suite dependency. Desktop/mobile checks use synthetic inventory data with polling disabled.

Before advertising a specific model as supported, obtain maintainer authorization for live LAN access, then record the model, firmware, configured transport, device instance, point types, and read-only request results with private details redacted. Verify a valid analog reading and its units, binary zero/one, multi-state values where present, fault/out-of-service behavior, a missing point, disconnect/recovery, history/CSV, and existing labels after restart. Do not alter controller configuration or create real faults as part of the test; use an administrator-provided test object or simulated evidence for unsafe cases.
