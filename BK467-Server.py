##
##  Coospo BK467 BLE Library
##  Created by nnnnnnn0090 on 2024/06/07.
##

import asyncio
import threading
import json
from enum import Enum
import bleak
from bleak import BleakClient, BleakScanner
from http.server import BaseHTTPRequestHandler, HTTPServer

# https://www.bluetooth.com/specifications/specs/gatt-specification-supplement-5/
# https://gist.github.com/sam016/4abe921b5a9ee27f67b3686910293026

# | name       |  type  |  size  |
# |------------|--------|--------|
# | Flag       | struct | 1 byte |
# | CumWheel   | uint32 | 4 byte | 
# | LastWheel  | uint32 | 4 byte |
# | CumCrank   | uint32 | 4 byte |
# | LastCrankTm| uint32 | 4 byte |

# Flag Structure
  # bit 0  : Wheel Revolution Data is present
  # bit 1  : Crank Revolution Data is present
  # bit 2-7: Unused

class Device_Information(Enum):
    Manufacturer_Name_String = "00002a29-0000-1000-8000-00805f9b34fb"
    Model_Number_String = "00002a24-0000-1000-8000-00805f9b34fb"
    Hardware_Revision_String = "00002a27-0000-1000-8000-00805f9b34fb"
    Software_Revision_String = "00002a28-0000-1000-8000-00805f9b34fb"

class Generic_Access_Profile(Enum):
    Device_Name = "00002a00-0000-1000-8000-00805f9b34fb"
    Appearance = "00002a01-0000-1000-8000-00805f9b34fb"
    Manufacturer_Name_String = "00002a04-0000-1000-8000-00805f9b34fb"
    Central_Address_Resolution = "00002aa6-0000-1000-8000-00805f9b34fb"
    
class Battery_Service(Enum):
    Battery_Level = "00002a19-0000-1000-8000-00805f9b34fb"

class Cycling_Speed_and_Cadence(Enum):
    CSC_Feature = "00002a5c-0000-1000-8000-00805f9b34fb"
    Sensor_Location = "00002a5d-0000-1000-8000-00805f9b34fb"
    CSC_Measurement_Notify = "00002a5b-0000-1000-8000-00805f9b34fb"


class BK467:
    def __init__(self):
        self.client: BleakClient = None
        self.data = [0,0,0,0,0,0,0]

    @staticmethod
    async def scan_async():
        print("Searching BK467...")
        return [d for d in await BleakScanner.discover() if d.name and "BK6" in d.name]

    async def connect(self, mac_address):
        client = BleakClient(mac_address, timeout=100000)
        await client.connect()
        if self.client != None: return
        self.client = client
        device_name = (await self.get_attr(Generic_Access_Profile.Device_Name)).decode()
        print(f"Connected: {client.address}, {device_name}")
        await client.start_notify(Cycling_Speed_and_Cadence.CSC_Measurement_Notify.value, self.speed_and_cadence_notify)

    async def _check_device_connected(self):
        if self.client == None:
            raise ValueError("Device not connected")
        
    def speed_and_cadence_notify(self, sender, data: bytearray):
        self.data = data
        # print(data.hex())
        # print(data[2:-1].hex())

    async def get_attr(self, key):
        await self._check_device_connected()
        data = await self.client.read_gatt_char(key.value)
        return data
    
    async def get_battery_level(self):
        await self._check_device_connected()
        data = await self.client.read_gatt_char(Battery_Service.Battery_Level.value)
        return int(data[0])
    
    async def get_mode(self):
        await self._check_device_connected()
        is_speed_mode = (self.data[0] & 0x01) > 0
        is_cadence_mode = (self.data[0] & 0x02) > 0
        return 'cadence' if is_cadence_mode else 'speed'
        
    prev_combo_csc_cum_wheel_rev = 0
    prev_combo_csc_wheel_time = 0
    
    async def get_wheel_rpm(self):
        await self._check_device_connected()
        cum_wheel_rev = await self.get_cum_wheel_rev()
        last_wheel_time = await self.get_last_wheel_time()
        
        wheel_rpm = 0.0
        
        delta_rotations = cum_wheel_rev - self.prev_combo_csc_cum_wheel_rev
        time_delta = last_wheel_time - self.prev_combo_csc_wheel_time
        if time_delta < 0:
            time_delta += 65535
        
        if time_delta != 0 and self.prev_combo_csc_cum_wheel_rev != 0:
            wheel_rpm = (2048.0 if False else 1024.0) * (delta_rotations * 60.0) / time_delta
        
        self.prev_combo_csc_cum_wheel_rev = cum_wheel_rev
        self.prev_combo_csc_wheel_time = last_wheel_time
        
        return wheel_rpm
    
    async def get_wheel_speed(self):
        rpm = await self.get_wheel_rpm()
        wheel_circumference = 0.622 * 3.14159265359
        rps = rpm / 60
        mps = wheel_circumference * rps
        kmph = mps * 3.6
        return (kmph, rpm)
    
    async def get_cum_wheel_rev(self):
        await self._check_device_connected()
        if await self.get_mode() != "speed": return 0
        return (self.data[4] << 24) + (self.data[3] << 16) + (self.data[2] << 8) + self.data[1]
    
    async def get_last_wheel_time(self):
        await self._check_device_connected()
        if await self.get_mode() != "speed": return 0
        return (self.data[6] << 8) + self.data[5]

    async def get_cadence(self):
        await self._check_device_connected()
        if await self.get_mode() != "cadence": return 0
        return self.calculate_cadence(await self.get_cum_crank_rev(), await self.get_last_crank_time())
    
    async def get_cum_crank_rev(self):
        await self._check_device_connected()
        if await self.get_mode() != "cadence": return 0
        return (self.data[2] << 8) + self.data[1]
    
    async def get_last_crank_time(self):
        await self._check_device_connected()
        if await self.get_mode() != "cadence": return 0
        return (self.data[4] << 8) + self.data[3]
    
    prev_cum_crank_rev = 0
    prev_crank_time = 0
    prev_crank_staleness = 0
    prev_rpm = 0.0

    def calculate_cadence(self, cum_crank_rev, last_crank_time):
        delta_rotations = cum_crank_rev - self.prev_cum_crank_rev
        if delta_rotations < 0:
            delta_rotations += 65535

        time_delta = last_crank_time - self.prev_crank_time
        if time_delta < 0:
            time_delta += 65535

        if time_delta != 0:
            self.prev_crank_staleness = 0
            time_mins = time_delta / 1024.0 / 60.0
            rpm = delta_rotations / time_mins
            self.prev_rpm = rpm
        elif time_delta == 0 and self.prev_crank_staleness < 2:
            rpm = self.prev_rpm
            self.prev_crank_staleness += 1
        elif self.prev_crank_staleness >= 2:
            rpm = 0.0

        self.prev_cum_crank_rev = cum_crank_rev
        self.prev_crank_time = last_crank_time

        def check_rpm(rpm):
            if rpm <= 500:
                return rpm
            else:
                return 0.0
            
        return check_rpm(rpm)

    async def test(self):
        await self._check_device_connected()


async def main():
    HOST = '0.0.0.0'
    PORT = 10000

    bk467 = BK467()
    device_name = ""
    battery_level = 0
    json_data = "{}"

    async def on_connected():
        nonlocal device_name, battery_level
        device_name = (await bk467.get_attr(Generic_Access_Profile.Device_Name)).decode()
        print("Device_Name:", device_name)
        battery_level = await bk467.get_battery_level()
        print("Battery_Level:", battery_level)

    async def generate_json_str():
        nonlocal device_name, battery_level
        wheel_speed = await bk467.get_wheel_speed()
        w_data = {
            'device_name': device_name,
            'battery_level': battery_level,
            'mode': await bk467.get_mode(),
            'cadence': await bk467.get_cadence(),
            'cum_crank_rev': await bk467.get_cum_crank_rev(),
            'last_crank_time': await bk467.get_last_crank_time(),
            'last_crank_time_sec': int(await bk467.get_last_crank_time() / 1024),
            'cycling_speed': wheel_speed[0],
            'cycling_rpm': wheel_speed[1],
            'cum_wheel_rev': await bk467.get_cum_wheel_rev(),
            'last_wheel_time': await bk467.get_last_wheel_time(),
            'last_wheel_time_sec': int(await bk467.get_last_wheel_time() / 1024)
        }
        json_string = json.dumps(w_data, indent=2)
        return json_string

    class MyHTTPRequestHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json_data.encode())
        def log_message(self, format, *args):
            return

    async def update():
        nonlocal json_data
        while True:
            json_data = await generate_json_str()
            await asyncio.sleep(1)

    def start_server():
        server = HTTPServer((HOST, PORT), MyHTTPRequestHandler)
        print(f'Starting server on {HOST}:{PORT}...')
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print('Server stopped.')
            server.server_close()

    while True:
        devices = await bk467.scan_async()
        if len(devices) != 0:
            await bk467.connect(devices[0].address)
            await on_connected()
            break
            
    asyncio.create_task(update())
    thread1 = threading.Thread(target=start_server)
    thread1.start()

    while True:
        await asyncio.sleep(1)

asyncio.run(main())

# # 00001800-0000-1000-8000-00805f9b34fb: Generic Access Profile: ["['read', 'write'],00002a00-0000-1000-8000-00805f9b34fb", "['read'],00002a01-0000-1000-8000-00805f9b34fb", "['read'],00002a04-0000-1000-8000-00805f9b34fb", "['read'],00002aa6-0000-1000-8000-00805f9b34fb"]
# Device Name, Appearance, "Manufacturer Name String", Central Address Resolution

# # 00001801-0000-1000-8000-00805f9b34fb: Generic Attribute Profile: []

# # 00001816-0000-1000-8000-00805f9b34fb: Cycling Speed and Cadence: ["['notify'],00002a5b-0000-1000-8000-00805f9b34fb", "['read'],00002a5c-0000-1000-8000-00805f9b34fb", "['read'],00002a5d-0000-1000-8000-00805f9b34fb", "['write', 'indicate'],00002a55-0000-1000-8000-00805f9b34fb"]
# CSC Feature, Sensor Location

# # 0000180f-0000-1000-8000-00805f9b34fb: Battery Service: ["['read', 'notify'],00002a19-0000-1000-8000-00805f9b34fb"]  // Battery Level

# # 0000fd00-0000-1000-8000-00805f9b34fb: Vendor specific: ["['notify'],0000fd09-0000-1000-8000-00805f9b34fb", "['write-without-response'],0000fd0a-0000-1000-8000-00805f9b34fb", "['notify'],0000fd19-0000-1000-8000-00805f9b34fb", "['write-without-response'],0000fd1a-0000-1000-8000-00805f9b34fb"]

# # 0000180a-0000-1000-8000-00805f9b34fb: Device Information: ["['read'],00002a29-0000-1000-8000-00805f9b34fb", "['read'],00002a24-0000-1000-8000-00805f9b34fb", "['read'],00002a27-0000-1000-8000-00805f9b34fb", "['read'],00002a28-0000-1000-8000-00805f9b34fb"]