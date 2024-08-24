
# OBD2 to MQTT Bridge

Following Jaguar Land Rover's recent lockdown of their API, I put this this script together to help at least get EV State of Charge and other basic data from an I-Pace when it is connected to the home WiFi. The script basically provides a bridge between a WiFi version of an EL327 OBD-II interface, such as the VGate iCar Pro, and an MQTT broker, such that the data can then be used by home automation systems, such as openHAB.

Credit to [dconlon](https://github.com/dconlon) for his wifi version of the OBD python library.


## Requirements

- Python 3.6+
- paho-mqtt
- configparser
- pint
- WiFi version of the OBD library from [https://github.com/dconlon/icar_odb_wifi](https://github.com/dconlon/icar_odb_wifi)


## Installation

1. Clone this repository:
   ```
   git clone https://github.com/yourusername/obd2mqtt.git
   cd obd2mqtt
   ```

2. Install the required Python packages:
   ```
   pip install paho-mqtt configparser pint
   ```

3. Install the WiFi version of the OBD library:
   ```
   pip install git+https://github.com/dconlon/icar_odb_wifi.git
   ```

4. Copy the sample configuration file and modify it according to your setup:
   ```
   cp obd2mqtt.cfg.sample obd2mqtt.cfg
   ```

## Configuration

Edit the `obd2mqtt.cfg` file to set up your MQTT broker details and OBD-II interface:

```ini
[MQTT]
MQTT_SERVER = your_mqtt_broker_address
MQTT_SUB_TOPIC = obd2mqtt/commands
MQTT_PUB_TOPIC = obd2mqtt/data
MQTT_USER = your_mqtt_username
MQTT_PASSWORD = your_mqtt_password

[OBD]
OBD_PORT = your_obd_wifi_address

[MISC]
DEBUG = False
```

## Usage

Run the script:

```
python obd2mqtt.py
```

The script will connect to the OBD-II's WiFi interface and the MQTT broker. It will then listen for commands on the specified MQTT subscription topic and publish vehicle data to the publication topic.

## PIDs

The `obd2_pids.csv` file includes an initial set of PIDs from a Jaguar I-Pace.

This file can be customised as required for the PIDs supported by whatever vehicle the OBD is connected to. Each line should contain the following fields:

```
pid,name,description,decoder_function,units
```

The `docoder_function` is the name of the function to decode the type of data returned by the obd library. The current decoder functions included (over and above those built-in into the obd library such as `raw_string`) are ones I needed for decoding the PIDs from the I-Pace:
```
Function Name            | Description
-------------------------|----------------------------------------------------------
decode_int               | Decodes response as an integer (A)
decode_int_divide_by_2   | Decodes response as an integer divided by 2 (A/2)
decode_int_divide_by_10  | Decodes response as an integer divided by 10 (A/10)
decode_int_divide_by_100 | Decodes response as (256*A + B)/100
decode_int_divide_by_108 | Decodes response as an integer divided by 108 (A/108)
decode_int_divide_by_1000| Decodes response as (256*A + B)/1000
decode_temperature       | Decodes response as temperature in Celsius (A-40)
decode_temperature2      | Decodes response as temperature in Celsius (A/2-40)
decode_temperature3      | Decodes response as temperature in Celsius (A-50)
decode_current           | Decodes response as current in amps ((A*256+B-32768)/40)
decode_location          | Decodes response as GPS latitude and longitude

```
Note:
- A, B represent the first and second bytes of the OBD-II response, respectively.
- These functions handle different scaling factors and offsets as used for the I-Pace PIDs given.


## MQTT Commands

To interact with the OBD2 to MQTT Bridge, JSON-formatted commands can be published to the MQTT subscription topic as specified in the configuration file (default is `ipace/obd2mqtt/_system/send_command`).

### Available Commands:

1. **refresh_all**: Queries all standard set of PIDs and publish their values.
   ```json
   {"command": "refresh_all"}
   ```

2. **available_commands**: Lists all available OBD commands.
   ```json
   {"command": "available_commands"}
   ```

3. **reload_pids**: Reloads the PIDs from the PIDs `csv` file.
   ```json
   {"command": "reload_pids"}
   ```

4. **set_obd_mode**: Enables or disables the OBD interface power saving (i.e. leave the OBD2 device permanently on, or allow to go to sleep). _Please see the comments to the code for this, as there the actual AT command value can be tweaked depending on what is required._

   ```json
   {
     "command": "set_obd_mode",
     "mode": "enable"  // or "disable"
   }
   ```

5. **Custom OBD Commands**: Query the value of any single PID from the csv file using its "name".
   ```json
   {"command": "RPM"}  // Example: Get engine RPM
   ```

6. **custom**: Sends a custom PID command.
   ```json
   {
     "command": "custom",
     "definition": {
       "name": "CustomCommand",
       "desc": "Description of the command",
       "pid": "ABCD",
       "decoder": "decode_int",
       "units": "rpm"
     }
   }

   ```
   NOTE `pid` and `decoder` are mandatory when sending custom PID commands



## Contributing

The script seems to be working for my purposes, and so I am not intending to spend much time developing it further. Contributions are however always welcome; please feel free to submit a Pull Request!

## License

This project is licensed under the MIT License.
