# Terminal Adapter

Two implementations controlled by `terminal_mode` in environment config.

## SimulatedTerminalAdapter
Software EMV terminal simulator. No hardware required.

## PhysicalTerminalAdapter
Connects to a real POS terminal via serial or TCP.
Designed to work with a robotic arm rig for automated physical button pressing.

## Config

```json
{
  "terminal_mode": "simulated",
  "host": "192.168.1.100",
  "port": 5000,
  "serial_port": "/dev/ttyUSB0",
  "baud_rate": 115200
}
```

## Supported Actions

| Action | Description |
|---|---|
| `insert_card` | Insert a card profile into the terminal |
| `tap_nfc` | Simulate NFC contactless tap |
| `enter_pin` | Enter PIN digits |
| `remove_card` | Remove the card |
| `read_display` | Read current terminal display text |
| `read_receipt` | Read the printed receipt content |
| `press_key` | Press a specific key (ENTER, CANCEL, numeric) |
