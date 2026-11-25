"""Send ten synthetic HL7 v2.5.1 demo messages to the local MLLP listener."""

from datetime import UTC, datetime, timedelta
from socket import create_connection


def segment(name: str, fields: dict[int, str]) -> str:
    values = [""] * (max(fields) + 1)
    values[0] = name
    for index, value in fields.items():
        values[index] = value
    return "|".join(values)


def demo_messages(now: datetime) -> list[tuple[str, str]]:
    def timestamp(value: datetime) -> str:
        return value.strftime("%Y%m%d%H%M%S%z")

    people = [
        ("Avery", "GENERAL", 0, "I"),
        ("Blake", "GENERAL", 1, "I"),
        ("Casey", "COMMUNITY", 2, "I"),
        ("Devon", "GENERAL", 3, "O"),
        ("Ellis", "COMMUNITY", 14, "I"),
    ]

    def message(
        index: int, sender: str, event: str, sent_at: datetime, suffix: list[str],
    ) -> tuple[str, str]:
        given, facility, age, patient_class = people[index]
        sent = timestamp(sent_at)
        control = f"UI-DEMO-{event.replace('^', '-')}-{index + 1:02d}"
        visit = {1: "1", 2: patient_class, 3: "WARD^101^A", 19: f"UI-DEMO-V{index + 1:02d}",
                 44: timestamp(now - timedelta(days=age, hours=2))}
        if event == "ADT^A03":
            visit[45] = sent
        segments = [
            rf"MSH|^~\&|{sender}|{facility}|||{sent}||{event}|{control}|P|2.5.1",
            segment("PID", {1: "1", 3: f"UI-DEMO-{index + 1:02d}^^^{facility}^MR",
                            5: f"Demo^{given}", 7: "19800101"}),
            segment("PV1", visit),
        ]
        if event.startswith("ADT"):
            segments.insert(1, segment("EVN", {1: event.split("^")[1], 2: sent}))
        return control, "\r".join([*segments, *suffix, ""])

    messages = [
        message(i, "DEMO_EHR", "ADT^A01" if patient_class == "I" else "ADT^A04",
                now - timedelta(days=age, hours=2), [])
        for i, (_, _, age, patient_class) in enumerate(people)
    ]
    messages.append(message(2, "DEMO_EHR", "ADT^A03", now - timedelta(days=1), []))
    for i, value in enumerate(("1.8", "0.9", "2.1")):
        observed = now - timedelta(days=i, hours=1)
        messages.append(message(i, "DEMO_LIS", "ORU^R01", observed, [
            segment("OBR", {1: "1", 3: f"DEMO-LAB-{i + 1}",
                            4: "2160-0^Creatinine^LN", 7: timestamp(observed),
                            16: "PROVIDER123^Chen^Maya"}),
            segment("OBX", {1: "1", 2: "NM", 3: "2160-0^Creatinine^LN",
                            5: value, 6: "mg/dL", 11: "F", 14: timestamp(observed)}),
        ]))
    messages.append(message(3, "DEMO_RIS", "ORM^O01", now - timedelta(days=2), [
        segment("ORC", {1: "NW", 2: "DEMO-CT-1"}),
        segment("OBR", {1: "1", 2: "DEMO-CT-1", 4: "IMG200^CT chest without contrast",
                        16: "PROVIDER456^Patel^Anika"}),
    ]))
    # Send the discharge after its older lab result so the current visit remains discharged.
    messages.append(messages.pop(5))
    return messages


def main() -> None:
    with create_connection(("127.0.0.1", 2575), timeout=10) as client:
        for control, payload in demo_messages(datetime.now(UTC)):
            client.sendall(b"\x0b" + payload.encode() + b"\x1c\r")
            ack = b""
            while not ack.endswith(b"\x1c\r"):
                chunk = client.recv(4096)
                if not chunk:
                    raise RuntimeError(f"No complete ACK for {control}")
                ack += chunk
            if f"\rMSA|AA|{control}|".encode() not in ack:
                raise RuntimeError(f"Receiver rejected {control}")
            print(ack.decode().split("\r")[1])


if __name__ == "__main__":
    main()
