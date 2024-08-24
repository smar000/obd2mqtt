#! /usr/bin/env python3

import asyncio
import json
import time
import datetime
import re
import paho.mqtt.publish as publish
import paho.mqtt.client as mqtt
import os, sys, signal
import logging
import traceback
import configparser
import csv
import os
from collections import namedtuple
import pint
import obd
from obd.OBDCommand import OBDCommand
from obd.decoders import percent, sensor_voltage_big, sensor_voltage, raw_string
from obd.protocols import ECU


VERSION     = "0.8"
CONFIG_FILE = "obd2mqtt.cfg"
PID_FILE    = "obd2_pids.csv"


logger = logging.getLogger('obd2mqtt')
ch = logging.StreamHandler(sys.stdout)
ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)8s [%(lineno)4d] %(message)s"))
logger.addHandler(ch)
logger.propagate = False

os.chdir(os.path.dirname(os.path.realpath(__file__)))
if not os.path.isfile(CONFIG_FILE):
    logger.critical(f"The configuration file '{CONFIG_FILE}' does not exist.")
    sys.exit(0)


def get_config_param(config,section,name,default):
    """
    Retrieve a configuration parameter from the specified section and name.
    If the parameter doesn't exist, return the default value.
    """
    if config.has_option(section,name):
        return config.get(section,name)
    else:
        return default

config = configparser.RawConfigParser()
config.read(CONFIG_FILE)


DEBUG = bool(get_config_param(config,"MISC", "DEBUG", False) == "True")
logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)


MQTT_SERVER       = get_config_param(config,"MQTT", "MQTT_SERVER", "")
MQTT_SUB_TOPIC    = get_config_param(config,"MQTT", "MQTT_SUB_TOPIC", "").rstrip('/')
MQTT_PUB_TOPIC    = get_config_param(config,"MQTT", "MQTT_PUB_TOPIC", "").rstrip('/')
MQTT_USER         = get_config_param(config,"MQTT", "MQTT_USER", "")
MQTT_PW           = get_config_param(config,"MQTT", "MQTT_PASSWORD", "")
MQTT_CLIENTID     = get_config_param(config,"MQTT", "MQTT_CLIENTID", "obd2mqtt")
MQTT_QOS          = 0
MQTT_RETAIN       = bool(get_config_param(config,"MQTT", "MQTT_RETAIN", False) == "True")

STATUS_STATE_ATTR = "state"

OBD_SYSTEM_SUBTOPIC = "_system"
OBD_SYSTEM_SENSOR_TYPE = "system"
OBD_SYSTEM_TOPIC = "{}/{}".format(MQTT_PUB_TOPIC, OBD_SYSTEM_SUBTOPIC)

OBD_PORT     = get_config_param(config,"OBD", "OBD_PORT", "")
OBD_STATUS_PORT_CONNECTED = "OBD Connected"
OBD_STATUS_CAR_CONNECTED = "Car Connected"

obd_connection = None
obd_port_connected = False
obd_car_connected = False

obd_last_query_dtm = None

pid = namedtuple('obd_command', ['pid', 'name', 'desc', 'decoder', 'units', 'include_in_status_refresh'])
obd_commands = {}


def publish_status_attributes(attributes, subtopic=None):
    """
    Publish status attributes to MQTT topics.

    Args:
    attributes (dict): A dictionary of attributes to publish
    subtopic (str, optional): A subtopic to append to the MQTT_PUB_TOPIC
    """
    try:
        topic = "{}/{}".format(MQTT_PUB_TOPIC, subtopic) if subtopic else MQTT_PUB_TOPIC
        for k, v in  attributes.items():
            if not isinstance(v, list):
                try:
                    if "pint.util.Quantity" in str(type(v)):
                        value = v.magnitude
                        units = str(v.units)
                    else:
                        value = v
                    mqtt_client.publish("{}/{}".format(topic, k),value, 0, True)
                except Exception as e:
                    logger.error("Error: {}, k: {}, v: {}, type(v): {}".format(e, k, v, type(v)))
        mqtt_client.publish("{}/_refresh_ts".format(topic), datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),0,True)
        mqtt_client.publish("{}/last_update".format(OBD_SYSTEM_SUBTOPIC), datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),0,True)
    except Exception as e:
        _, _, exc_tb = sys.exc_info()
        logger.error("{} (line {})".format(e, exc_tb.tb_lineno))
        # logger.error("Error: publish_status_attributes called with attributes: {}".format(attributes))


def sigterm_handler(_signo, _stack_frame):
    """
    Signal handler for SIGTERM. Calls exit_gracefully() and exits the program.
    """
    exit_gracefully()
    sys.exit(0)


def exit_gracefully():
    """
    Perform cleanup operations before exiting the program.
    Disconnects from MQTT and updates the state to 'offline'.
    """
    if mqtt_client:
        update_state_on_mqtt("offline")
        mqtt_client.loop_stop()
        mqtt_client.disconnect()


def initialise_mqtt_client(mqtt_client):
    """
    Initialize the MQTT client with the necessary settings and callbacks.

    Args:
    mqtt_client: The MQTT client object to initialize

    Returns:
    The initialized MQTT client
    """
    #  print("init")
    if MQTT_USER:
        mqtt_client.username_pw_set(MQTT_USER, MQTT_PW)
    mqtt_client.on_connect = mqtt_on_connect
    mqtt_client.on_message = mqtt_on_message
    # mqtt_client.on_log = mqtt_on_log
    mqtt_client.on_disconnect = mqtt_on_disconnect

    logger.info("Connecting to MQTT server %s" % MQTT_SERVER)
    mqtt_client.connect(MQTT_SERVER, port=1883, keepalive=60, bind_address="")
    logger.info("MQTT connection done")
    return mqtt_client


def mqtt_on_connect(client, userdata, flags, rc):
    """
    Callback function for MQTT connection event processing

    Args:
    client: The MQTT client instance
    userdata: The private user data as set in Client() or user_data_set()
    flags: Response flags sent by the broker
    rc: The connection result
    """

    if rc == 0:
        client.connected_flag = True #set flag
        logger.info("MQTT connection established with broker")
        update_state_on_mqtt("online")

        logger.info("Subscribing to mqtt topic '%s' for inbound commands" % MQTT_SUB_TOPIC)
        mqtt_client.subscribe(MQTT_SUB_TOPIC)

    else:
        logger.error("MQTT connection failed (code {})".format(rc))
        logger.debug(" mqtt userdata: {}, flags: {}, client: {}".format(userdata, flags, client))


def mqtt_on_disconnect(client, userdata, rc):
    """
    Callback function for MQTT disconnection event processing

    Args:
    client: The MQTT client instance
    userdata: The private user data as set in Client() or user_data_set()
    rc: The disconnection result
    """
    client.connected_flag = False
    update_state_on_mqtt("offline")
    client.loop_stop()
    if rc != 0:
        logger.warning("Unexpected disconnection. Attempting reconnect...")
        logger.debug("[DEBUG] mqtt rc: {}, userdata: {}, client: {}".format(rc, userdata, client))


def mqtt_on_log(client, obj, level, string):
    """
    Callback function for MQTT log event received

    Args:
    client: The MQTT client instance
    obj: The private user data as set in Client() or user_data_set()
    level: The severity of the message
    string: The log message
    """
    logger.debug ("[DEBUG] MQTT log message received. Client: {}, obj: {}, level: {}".format(client, obj, level))
    logger.debug("[DEBUG] MQTT log msg: {}".format(string))


def mqtt_on_message(client, userdata, msg):
    """
    Callback function for MQTT message received on subscribed topic

    Args:
    client: The MQTT client instance
    userdata: The private user data as set in Client() or user_data_set()
    msg: An instance of MQTTMessage containing topic and payload
    """

    logger.info("Incoming MQTT message: {}".format(msg.payload))
    try:
        payload = str(msg.payload, 'utf-8')
        json_data = json.loads(payload)
        if "command" in json_data:
            mqtt_client.publish("{}/send_command_ts".format(OBD_SYSTEM_TOPIC), get_timestamp_string(), 0, True)
            process_command(json_data)
        else:
            mqtt_client.publish("{}/send_command_response_ts".format(OBD_SYSTEM_TOPIC),"", 0, True)
            logger.error("Command not recognised: {}".format(json_data))

    except Exception as e:
        _, _, exc_tb = sys.exc_info()
        logger.error("{} (line {})".format(e, exc_tb.tb_lineno))
        logger.error("msg: {} (payload str: '{}')".format(msg, payload))
        error_msg = {STATUS_STATE_ATTR: "Error", "msg" : {}, "errorDescription": "{} (line {})".format(e, msg, exc_tb.tb_lineno)}
        publish_command_response("{}".format(error_msg))


def update_state_on_mqtt(state):
    """
    Update the state on MQTT and publish related information

    Args:
    state (str): The state to update ('online' or 'offline')
    """
    mqtt_client.publish("{}/state".format(OBD_SYSTEM_TOPIC), state, MQTT_QOS, True)
    mqtt_client.publish("{}/config".format(OBD_SYSTEM_TOPIC), '{"version":"' + VERSION + '"}', MQTT_QOS, True)
    mqtt_client.publish("{}/system_state_updated".format(OBD_SYSTEM_TOPIC),  get_timestamp_string(), MQTT_QOS, True)


def get_timestamp_string():
    """
    Get the current timestamp as a formatted string

    Returns:
    str: The current timestamp in the format "YYYY-MM-DDTHH:MM:SSZ"
    """
    return datetime.datetime.now().strftime("%Y-%m-%dT%XZ")


def publish_command_response(status):
    """
    Publish the command response to MQTT

    Args:
    status: The status or response to publish
    """
    mqtt_client.publish("{}/send_command_response".format(OBD_SYSTEM_TOPIC), "{}".format(status), 0, True)
    mqtt_client.publish("{}/send_command_response_ts".format(OBD_SYSTEM_TOPIC), get_timestamp_string(), 0, True)


def log_system_error(error, line_number=None):
    """
    Log a system error and publish it to MQTT

    Args:
    error: The error message
    line_number (optional): The line number where the error occurred
    """
    desc = "{} (line {})".format(error, line_number) if line_number else "{}".format(error)
    logger.error(desc)
    msg = {STATUS_STATE_ATTR: "Error", "errorDescription": desc}
    publish_command_response(msg)


def do_command(cmd):
    """
    Execute an OBD command and return the response

    Args:
    cmd: The OBD command to execute

    Returns:
    The response from the OBD command, or None if there's an error
    """
    if not initialise_obd():
        logger.warning(f"Cannot process command as connection not available. Current status is {obd_connection.status()}")
        return

    try:
        response = obd_connection.query(cmd, force=True)
        obd_last_query_dtm = datetime.datetime.now()
        value = str(response.value).replace("\n", ", ")
        logger.debug(f'  -- Response: {value}')
        return response

    except Exception as e:
        _, _, exc_tb = sys.exc_info()
        logger.error("{} (line {})".format(e, exc_tb.tb_lineno))



def set_obd_mode(mode):
    """
        Set the OBD mode (enable or disable)

        Args:
        mode (str): The mode to set ('enable' or 'disable')

        Returns:
        The response from setting the OBD mode
        ----------------------------------------

        https://www.elmelectronics.com/DSheets/ELM327DSH.pdf - Page 54 Programmable Parameters
        Enables or disables the auto-sleep mode of the Vgate Car Pro OBD interface
        https://github.com/dconlon/icar_odb_wifi
        Disable sleep (SV = Set Value):     send ATPP 0E SV 7A
        Enable sleep:                       send ATPP 0E SV FA or possibly ATPP0ESVC6 (see note below)
        Save the changes: send ATPP 0E ON and then reboot

        # Out of the box my iCar had FA at PP0E (11111010), toggle first bit to
        # disable all low power (01111010 = 7A), back to FA to enable
        # elm327.send_and_parse(b"AT PP 0E SV 7A")

        # Note another comment: " The CORRECT FIX, imho, is to set 0E to C6, NOT FA. This will wake the device up 5 seconds after the ignition is turned to on, and put it to sleep when the ignition is turned to off,
        # thus not draining the battery. The CORRECTED command is ATPP 0E SV C6." - https://www.clarityforum.com/threads/obd-ii-sensor-goes-to-sleep-and-cant-get-up.1762/

        To get current status of all Programmable parameters:
            msgs = icar.interface.send_and_parse(b"AT PPS")
            for m in msgs:
                print(m.value)
        This returns a grid of values with suffixes  F=OFF, N=ON.

        Power Control options via the 0E param bits [factory default for the ELM327 9A (10011010) - note this may *not* be the case for the Vgate]:
            b7: Master enable   0: off 1: on             * if 0, pins 15 and 16 perform as described for v1.0 to v1.3a must be 1 to allow any Low Power functions
            b6: Pin 16 full power level 0: low 1: high   * normal output level, is inverted when in low power mode
            b5: Auto LP control 0: disabled 1: enabled   * allows low power mode if the RS232 activity stops
            b4: Auto LP timeout 0: 5 mins 1: 20 mins     * no RS232 activity timeout setting

            b3: Auto LP warning 0: disabled 1: enabled   * if enabled, says ‘ACT ALERT’ 1 minute before RS232 timeout
            b2: Ignition control 0: disabled 1: enabled  * allows low power mode if the IgnMon input goes low
            b1: Ignition delay 0: 1 sec 1: 5 sec         * delay after IgnMon (pin 15) returns to a high level, before normal operation resumes
            b0: reserved for future - leave set at 0     *

        For ease of ref (hex(int(binary_string,2)).upper()[2:]):
            7A: 0111 1010   - Turns off Master enable
            C6: 1100 0110   - Full power, 5mins LP timeout, and Ign control via IgnMon with 5 sec delay
            FA: 1111 1010   - Full power, Auto LP enabled,  20 mins LP timeout, 1 min warning, no ignition control, 5 sec delay on ign?
            AE: 1010 1110   - Low power, Auto LP, 5min timeout, 1 min warning, ign control, 5 sec ign delay

        NOTE: 2024/08/08 SR - For future ref, I am finding that AE works well for us.
    """

    if not connect_obd():
        logger.warning("Cannot process set_obd_mode command as connection to OBD not available")

    try:

        DISABLE = "AT PP 0E SV 7A"
        ENABLE = "AT PP 0E SV AE" #"ATPP0ESVFA"

        elm327 = obd_connection.interface
        mode_command = DISABLE if mode=="disable" else ENABLE
        mode_bytes = mode.encode("ascii")  # Convert to bytes
        elm327.send_and_parse(mode_bytes)  # Set the mode
        elm327.send_and_parse(b"ATPP0EON") # Save it

        logger.info(f"set_obd_mode command sent")
        return response

    except Exception as e:
        _, _, exc_tb = sys.exc_info()
        logger.error("{} (line {})".format(e, exc_tb.tb_lineno))


# async
def process_command(json_data):
    """
    Process the received command from MQTT

    Args:
    json_data (dict): The JSON data containing the command and other parameters

    Returns:
    The status or result of the command execution
    """
    logger.debug("Processing inbound command: '{}'".format(json_data))
    command = None

    try:
        publish_command_response({STATUS_STATE_ATTR : "process_command called"})
        command = json_data["command"]

        response = None
        global obd_connection
        global obd_commands
        if command == "available_commands":
            connection_ok = log_obd_port_status()
            if connection_ok:

                logger.info(f"Supported commands:")
                for cmd in obd_connection.supported_commands:
                    logger.info(f"-- {cmd.name}")
                    mqtt_client.publish(f"{OBD_SYSTEM_TOPIC}/_available_commands/", cmd.name, 0, True)
            else:
                status = "Unable to retrieve commands as OBD connection is not available"

        elif command == "reload_pids":
            initialise_obd(force=True)
            return

        elif "set_obd_mode" in command:
            if not "mode" in json_data:
                logger.warning(f"set_obd_mode requires 'mode' to be specified (one of 'enabled' or 'disabled')")
                return
            response = set_obd_mode(json_data["mode"])

        elif command == "refresh_all":
            connection_ok = log_obd_port_status()
            if connection_ok:
                # Loop through standard set of status queries
                if not initialise_obd():
                    return
                status = ""
                for k, cmd in obd_commands.items():
                    if cmd.include_in_status_refresh:
                        status += str(process_command({"command": cmd.name})) + ", "
                status = status.strip()[:-1]
            else:
                status = "OBD connection not available"

        elif command.upper() in obd_commands:
            name = command.replace("get_","")
            cmd = get_command(name)
            if not cmd:
                logger.warning(f"The command '{name}' does not seem to be valid")
                logger.info(f"Supported commands:")
                for cmd in obd_connection.supported_commands:
                    logger.info(f"-- {cmd.name}")
                return

            response = do_command(cmd)

        elif command == "custom": # TODO.... Broken...
            if not "definition" in json_data or not "pid" in json_data["definition"] or not "decoder" in json_data["definition"]:
                logger.warning("Custom commands require '\"definion\": {\"name\": \"Short name of command\", \"desc\": \"Description of the command\", \"pid\": \"ABFF\", \"decoder\": \"decoder function\", \"units\": \"volt\"}}' json")
                return

            cmd_json = json_data["definition"]
            pid = cmd_json["pid"].upper().strip()
            name = cmd_json["name"].upper().strip() if "name" in cmd_json else f"_custom_pid_{pid}"
            desc = cmd_json["desc"] if "desc" in cmd_json else name

            units = cmd_json["units"].strip() if "units" in cmd_json else ""
            fn_name = cmd_json["decoder"].strip()

            cmd = OBDCommand(name, desc, ("22" + pid).encode('utf-8'), 0, globals()[fn_name], fast=True)
            response = do_command(cmd)
            logger.info(f"  -- response.value.raw: {raw_message_formatter(response.value['raw'])},  response.value.value: {response.value['value']}")

        else:
            logger.error(f"The command '{command}' not recognised (mqtt message: {json_data})")
            status = {STATUS_STATE_ATTR: "Error", "errorDescription": "Command not recognised", "lastCommand" : command}

        units = ""
        if response:
            if "pint.util.Quantity" in str(type(response.value)):
                raw = response.value.magnitude
                value = response.value.magnitude
                units = str(response.value.units)
            else:
                logger.debug(f"response.value = '{response.value}'")
                logger.debug(f"type(response.value) = '{type(response.value)}'")
                if type(response.value) == dict and "raw" in response.value:
                    raw = response.value["raw"]
                    value = response.value["value"]
                elif type(response.value) == str or type(response.value) == int or type(response.value) == float:
                    raw = response.value
                    value = raw
                elif type(response.value) == "obd.protocols.protocol.Message":
                    logger.debug("  -- Message class object type identified..........")
                    raw = message.raw()
                    value = extract_number_value(raw)
                elif response.value != None and response.value != []:
                    logger.warning(f"  -- Could not process response (response.value={response.value})")
                    raw = response.value
                    value = response.value
                else:
                    raw = response.value
                    value = response.value

            if not units and cmd.name in obd_commands:
                units = obd_commands[cmd.name].units

            pid = obd_commands[command.upper()].pid if command.upper() in obd_commands else "----"
            try:
                logger.info(f"{pid} : {command.lower().ljust(20)}: {round(value,1) if type(value) == float else value} {units if (value or isinstance(value, (int, float, complex))) else ''}")
                json = {cmd.name.lower(): value, cmd.name.lower() + "_raw": raw_message_formatter(str(raw)), "units": units}
                publish_status_attributes(json, cmd.name.lower())
                status = raw
            except Exception as e:
                _, _, exc_tb = sys.exc_info()
                logger.error("{} (line {})".format(e, exc_tb.tb_lineno))
        else:
            logger.debug(f"  -- Response is empty")
            status = "Invalid response"

        return status

    except Exception as e:
        _, _, exc_tb = sys.exc_info()
        msg = {STATUS_STATE_ATTR: "Error", "errorDescription": "{} (line {})".format(e, exc_tb.tb_lineno), "lastCommand" : command}
        publish_command_response("{}".format(msg))
        logger.error("{} (line {})".format(e, exc_tb.tb_lineno))
        if "response" in locals(): logger.error(f"type(response.value) = {type(response.value)}")
        if command: logger.error("command: {} (json_data: '{}')".format(command,json_data))
        logger.error(traceback.format_exc())
        status = msg



def connect_obd(force=False):
    """
    This function attempts to connect to the OBD port.

    Args:
        force (bool, optional): If True, forces a reconnect even if a connection is already established. Defaults to False.

    Returns:
        tuple: A tuple containing two elements:
            - bool: True if the connection was successful, False otherwise.
            - bool: True if this connection attempt resulted in a new connection, False otherwise.
    """
    if not OBD_PORT:
        logger.critical("Cannot continue as OBD connection port is not defined in the config file")
        sys.exit(0)


    CONNECTION_TIMEOUT_MINS = 5

    global obd_last_query_dtm
    mins_since_last_query = (datetime.datetime.now() - obd_last_query_dtm).total_seconds() / 60 if obd_last_query_dtm else 999

    time_delta = datetime.timedelta(minutes=CONNECTION_TIMEOUT_MINS)
    delta_minutes = time_delta.total_seconds() / 60


    global obd_connection
    if force or not obd_connection or mins_since_last_query > delta_minutes or (not obd_is_connected()):
        logger.info("Reconnecting to OBD Port...")
        logger.debug(f"connect_obd: force={force},  mins_since_last_query = {mins_since_last_query} (obd_last_query_dtm: {obd_last_query_dtm})")
        logger.debug(f"  -- force: {force}")
        logger.debug(f"  -- mins_since_last_query > delta_minutes: {mins_since_last_query > delta_minutes}")
        logger.debug(f"  -- obd_is_connected(): {obd_is_connected()}")

        try:
            obd_connection = obd.OBD(OBD_PORT, 35000)
            obd_last_query_dtm = datetime.datetime.now()
            log_obd_port_status()
            result = obd_is_connected()
            connection_is_new = True
        except Exception as ex:
            logger.warning("Failed to connect to OBD port")
            result = False
            connection_is_new = False
    else:
        result = True
        connection_is_new = False

    return result, connection_is_new


def log_obd_port_status() -> None:
    """
    This function logs the status of the OBD port connection and vehicle ignition.

    Returns:
        None
    """
    if  obd_connection:
        logger.info(f"OBD Port connection status update: {obd_connection.status()}, Vehicle ignition active/connected: {obd_connection.is_connected()}")
        status = {"port_connected": obd_is_connected(), "ignition_on": obd_connection.is_connected()}
        publish_status_attributes(status, f"{OBD_SYSTEM_SUBTOPIC}/obd_status")
        return True
    else:
        status = {"port_connected": False, "ignition_on": False}
        publish_status_attributes(status, f"{OBD_SYSTEM_SUBTOPIC}/obd_status")

        return initialise_obd()



def get_command(name):
    """
    This function retrieves an OBD command object by name from the connected OBD adapter.

    Args:
        name (str): The name of the OBD command to retrieve.

    Returns:
        OBDCommand: The OBD command object if found, None otherwise.

    ----------------------
    Note obd_connection.supported_commands is a set. For checking if a command exists in obd_connection.supported_commands, we nedd to check for
    OBDCommand with 4  positional arguments: 'desc', 'command', '_bytes', and 'decoder'.  i.e.

    > OBDCommand('ELM_VERSION', 'ELM327 version string', b'ATI', 0, raw_string) in obd_connection.supported_commands
    > True
    """

    global obd_connection
    for cmd in obd_connection.supported_commands:
        if cmd.name.upper() == name.upper():
            return cmd
    return None


def signed_hex_to_decimal(hex_value, bits=8):
    # Calculate the maximum positive value for the given bit size
    max_positive = 2**(bits - 1) - 1
    # Calculate the maximum value for the given bit size
    max_value = 2**bits

    # Convert hex to decimal
    decimal_value = int(hex_value, 16)

    # If the value is greater than the maximum positive value, it is negative
    if decimal_value > max_positive:
        decimal_value -= max_value

    return decimal_value


def is_valid_hex(s):
    try:
        int(s, 16)
        return True
    except ValueError:
        return False


def decode_value(messages):
    """ Note we expect a collection of Message class objects
        This has .data in binary
    """

    try:
        if len(messages) == 0:
            logger.warning(f"No message data found (len(messages)=0))")
            return None

        # Juse use the data from the first ECU for now.
        print(messages[0])
        data = messages[0]
        print(data)
        # value = int.from_bytes(bytes.fromhex(data),"big")
        return data

    except Exception as e:
        _, _, exc_tb = sys.exc_info()
        logger.error("{} (line {})".format(e, exc_tb.tb_lineno))
        logger.error(f"len(messages): {len(messages)}")
        logger.error("messages: {}".format(messages))


def extract_number_value(messages_string):
    """ Note we expect the raw message string here, and not the Message class object
        The format of the raw message for the IPace seems to be:
            3 chars - ECU number
            4 chars - Unknown
            4 chars - PID
            Remaining chars - value data
    """

    if not messages_string:
        logger.warning("Cannot extract number value as messages_string is empty")
        return

    if type(messages_string) != str:
        logger.warning(f"type(messages_string) is not str (type is {type(messages_string)}. Dropping attempt to extract number value)")
        return messages_string

    messages = []
    try:
        messages = messages_string.split("\n") # In case of multiple messages (e.g. when multiple ECUs have the same PID)
        if len(messages) == 0:
            logger.warning(f"No message data found (messages_string = '{messages_string}')")
            return None

        # Just use the data from the first ECU for now.
        data = messages[0][11:]
        value = int.from_bytes(bytes.fromhex(data),"big")
        return value

    except Exception as e:
        _, _, exc_tb = sys.exc_info()
        logger.error("{} (line {})".format(e, exc_tb.tb_lineno))
        logger.error(f"len(messages): {len(messages)}")
        logger.error("messages_string: {}".format(messages_string))


def raw_message_formatter(messages_string):
    # Ensure the input string is of the expected length
    messages = []
    messages = messages_string.replace(" ", "").split("\n") # In case of multiple messages (e.g. when multiple ECUs have the same PID)
    formatted_messages = []
    for message in messages:
        if len(message) >= 11:
            # Slice the string at the required positions
            part1 = message[:3]
            part2 = message[3:7]
            part3 = message[7:11]
            part4 = message[11:]

            # Group the remaining part into sets of 2 digits
            grouped_part4 = ' '.join(part4[i:i+2] for i in range(0, len(part4), 2))

            # Concatenate the parts with spaces
            result = f"{part1} {part2} {part3} {grouped_part4}"
        else:
            result = message
        formatted_messages.append(result)

    result = ", ".join(formatted_messages)

    return result


def obd_is_connected():
    return obd_connection and (obd_connection.status() == OBD_STATUS_PORT_CONNECTED or obd_connection.status() == OBD_STATUS_CAR_CONNECTED)


def initialise_obd(force=False):
    logger.debug("initialise_obd called: Initalising OBD Connection and adding custom commands")
    connection_ok, is_new = connect_obd()
    if not connection_ok:
        logger.debug("OBD port could not be initalised")
        return False

    global obd_connection
    if not is_new and not force:
        return obd_is_connected()

    logger.info("OBD Port connected. Adding custom commands")

    global obd_commands
    obd_commands = {}
    include_in_full_status_update = ["ELM_VOLTAGE", "GET_CURRENT_DTC"] # Include these when we query all the standard PIDs via the "status" command

    # First add the built-in supported commands to our dict, before adding our own
    for cmd in obd_connection.supported_commands:
        obd_commands[cmd.name] = pid(str(cmd.pid), cmd.name, cmd.desc, cmd.decode, "", cmd.name in include_in_full_status_update)


    pid_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), PID_FILE)
    pid_data = []
    with open(pid_file_path, mode='r', newline='') as file:
        # Create a CSV reader object from the individual lines so that we can filter out anything starting with #
        lines = file.readlines()
        csv_reader = csv.DictReader(line for line in lines if not line.strip().startswith('#'))
        for row in csv_reader:
            stripped_row = {key.strip(): value.strip() for key, value in row.items()}
            pid_data.append(stripped_row)
    pid_data = sorted(pid_data, key=lambda pid_data: pid_data["name"])

    for row in pid_data:
        obd_commands[row["name"]]  = pid(row["pid"], row["name"],row["desc"], globals()[row["decoder_fn"]], row["units"], True)

    # Now add these to obd_commands supported command set
    for k, cmd in obd_commands.items():
        obd_connection.supported_commands.add(OBDCommand(cmd.name, cmd.desc, (f"22{cmd.pid}").encode('utf-8'), 0, cmd.decoder, fast=True))

    logger.info(f"PIDs from '{pid_file_path}' file added")

    return obd_is_connected()


def unpack_values(response, return_tuple=True):
    """ Unpack the A, B, C data values from a response callback Message object """

    # logger.info(f"type(response[0]) = '{type(response[0])}'")
    if type(response[0]) == "obd.protocols.protocol.Message":
        logger.debug("  -- Message class object type recognised..........")

    raw = response[0].raw()
    if "SEARCHING" in raw  or "NO DATA" in raw or raw == "?":
        logger.debug(f"  -- No data received to unpack (raw: '{raw}')")
        logger.debug(f"  -- response[0].value: {response[0].raw()}")
        return None

    hex_str = raw.split("\n")[0] # Default to just using the first message for now

    if len(hex_str) <= 10:
        logger.warning(f"  -- Could not unpack data as the size of the message string is less than 11 (message: '{hex_str}', raw: '{raw}', response[0]: '{response[0]}')")
        logger.warning(f"  -- response[0].raw: {response[0].raw()}")

        return None

    # Slice the string from the 11th character onwards
    sliced_str = hex_str[11:]

    if return_tuple:
        # Split the sliced string into two-character chunks
        chunks = [sliced_str[i:i+2] for i in range(0, len(sliced_str), 2)]
        logger.debug(f"  -- unpack_values('{raw_message_formatter(hex_str)}') returning values: {chunks}")
        # Convert the list of chunks to a tuple and return
        return tuple(chunks)
    else:
        logger.debug(f"  -- unpack_values('{raw_message_formatter(hex_str)}') returning values: {sliced_str}")
        # Convert the list of chunks to a tuple and return
        return sliced_str


def decode_int(response):
    """ Decode using formula: A """

    try:
        hex_str = unpack_values(response, False)
        # logger.info(f"  -- hex_str={hex_str}, type(hex_str): {type(hex_str)}")
        if hex_str:
            value = int(hex_str, 16)
        else:
            value = None
        response_raw = raw_message_formatter(response[0].raw())

        logger.debug(f"  -- decode_int('{response_raw}'): hex_str={hex_str}, value={value}")

        return {"raw": response[0].raw(), "value": value}

    except TypeError as e:
        pass
        # logger.error(f"Error: {e}")
    except Exception as ex:
        _, _, exc_tb = sys.exc_info()
        logger.error("{} (line {})".format(ex, exc_tb.tb_lineno))
        logger.error(f"hex_str: {hex_str}, response[0].raw(): {response[0].raw()}")


def decode_int_divide_by_2(response):
    """ Decode using formula: A/2 """

    try:
        a = unpack_values(response)[0]
        value = int(a, 16) /2
        response_raw = raw_message_formatter(response[0].raw())
        logger.debug(f"  -- decode_half_int('{response_raw}'): a={a}, value={value}")

        return {"raw": response[0].raw(), "value": value}
    except TypeError as e:
        logger.error(f"Error: {e}")
    except Exception as ex:
        _, _, exc_tb = sys.exc_info()
        logger.error("{} (line {})".format(ex, exc_tb.tb_lineno))


def decode_int_divide_by_10(response):
    """ Decode using formula: A/10 """

    try:
        a = unpack_values(response, False)
        value = int(a, 16)/10
        response_raw = raw_message_formatter(response[0].raw())
        logger.debug(f"  -- decode_int_divide_by_10('{response_raw}'): a={a}, value={value}")

        return {"raw": response[0].raw(), "value": value}
    except TypeError as e:
        pass
        # logger.error(f"Error: {e}")
    except Exception as ex:
        _, _, exc_tb = sys.exc_info()
        logger.error("{} (line {})".format(ex, exc_tb.tb_lineno))


def decode_int_divide_by_100(response):
    """ Decode using formula: (256*A + B)/100 """

    try:
        a, b = unpack_values(response)
    except TypeError as e:
        logger.error(f"Error: {e}")
        return

    value = ((256*int(a, 16)) + int(b, 16))/100
    response_raw = raw_message_formatter(response[0].raw())
    logger.debug(f"  -- decode_int_divide_by_100('{response_raw}'): a={a}, b={b}, value={value}")

    return {"raw": response[0].raw(), "value": value}


def decode_int_divide_by_108(response):
    """ Decode using formula: A/108 """

    try:
        a = unpack_values(response, False)
        value = int(a, 16)/108
        response_raw = raw_message_formatter(response[0].raw())
        logger.info(f"  -- decode_int_divide_by_108('{response_raw}'): a={a}, value={value}")

        return {"raw": response[0].raw(), "value": value}
    except TypeError as e:
        pass
        # logger.error(f"Error: {e}")
    except Exception as ex:
        _, _, exc_tb = sys.exc_info()
        logger.error("{} (line {})".format(ex, exc_tb.tb_lineno))



def decode_int_divide_by_1000(response):
    """ Decode using formula: (256*A + B)/1000 """

    try:
        a, b = unpack_values(response)
        value = ((256*int(a, 16)) + int(b, 16))/1000
        response_raw = raw_message_formatter(response[0].raw())
        logger.debug(f"  -- decode_int_divide_by_1000('{response_raw}'): a={a}, b={b}, value={value}")

        return {"raw": response[0].raw(), "value": value}

    except TypeError as e:
        logger.error(f"Error: {e}")
    except Exception as ex:
        _, _, exc_tb = sys.exc_info()
        logger.error("{} (line {})".format(ex, exc_tb.tb_lineno))



def decode_temperature(response):
    """ Decode using formula: A-40 """

    try:
        a = unpack_values(response)[0]
        value = int(a, 16) -40 if a else None
        response_raw = raw_message_formatter(response[0].raw())
        logger.debug(f"  -- decode_temperature('{response_raw}'): a={a}, value={value}")

        return {"raw": response[0].raw(), "value": value}
    except TypeError as e:
        logger.debug(f"Error: {e}")
    except Exception as ex:
        _, _, exc_tb = sys.exc_info()
        logger.error("{} (line {})".format(ex, exc_tb.tb_lineno))


def decode_temperature2(response):
    """ Decode using formula: A/2-40 """

    try:
        a = unpack_values(response)[0]
        value = int(a, 16)/2 -40 if a else None
        response_raw = raw_message_formatter(response[0].raw())
        logger.debug(f"  -- decode_temperature2('{response_raw}'): a={a}, value={value}")

        return {"raw": response[0].raw(), "value": value}
    except TypeError as e:
        logger.debug(f"Error: {e}")
    except Exception as ex:
        _, _, exc_tb = sys.exc_info()
        logger.error("{} (line {})".format(ex, exc_tb.tb_lineno))


def decode_temperature3(response):
    """ Decode using formula: A-50 """

    try:
        a = unpack_values(response)[0]
        value = int(a, 16) - 50 if a else None
        response_raw = raw_message_formatter(response[0].raw())
        logger.debug(f"  -- decode_temperature3('{response_raw}'): a={a}, value={value}")

        return {"raw": response[0].raw(), "value": value}
    except TypeError as e:
        logger.debug(f"Error: {e}")
    except Exception as ex:
        _, _, exc_tb = sys.exc_info()
        logger.error("{} (line {})".format(ex, exc_tb.tb_lineno))
        logger.error(f"a: {a}, response[0].raw(): {response[0].raw()}")


def decode_current(response):
    """ Decode using formula: (A*256+B-32768)/40 """

    try:
        a, b = unpack_values(response)
        value = ((256*int(a, 16)) + int(b, 16) - 32768)/40 if (a and b) else None
        response_raw = raw_message_formatter(response[0].raw())
        logger.debug(f"  -- decode_current('{response_raw}'): a={a}, b={b}, value={value}")

        return {"raw": response[0].raw(), "value": value}

    except TypeError as e:
        pass
        # logger.error(f"Error: {e}")
    except Exception as ex:
        _, _, exc_tb = sys.exc_info()
        logger.error("{} (line {})".format(ex, exc_tb.tb_lineno))



def decode_location(response):
    """ Decode using formulae:
            Lat : 754/a0a6 (byte(0-3)), signed divided by 0x20000
            Lon: 754/a0a6 (byte(4-7)), signed divided by 0x20000
    """

    try:
        a = str(unpack_values(response, False))
        if is_valid_hex(a):
            lat_bytes = a[:4]
            lng_bytes = a[4:]

            lat = signed_hex_to_decimal(lat_bytes) / int("20000", 16)
            lng = signed_hex_to_decimal(lng_bytes) / int("20000", 16)
            value = json.dumps({"lat": lat, "lng":lng})
            response_raw = raw_message_formatter(response[0].raw())
            logger.info(f"  -- decode_current('{response_raw}'): lat_bytes={lat_bytes}, lng_bytes={lng_bytes}, value={value}")
        else:
            value = "-,-"

        return {"raw": response[0].raw(), "value": value}


    except TypeError as e:
        pass
        # logger.error(f"Error: {e}")
    except Exception as ex:
        _, _, exc_tb = sys.exc_info()
        logger.error("{} (line {})".format(ex, exc_tb.tb_lineno))
        logger.error(f"  -- response[0].raw: {response[0].raw()}")
        logger.error(f"  -- a: {a}")



def main():
    print("...running obd2mqtt.py")
    signal.signal(signal.SIGTERM, sigterm_handler)
    if not MQTT_SERVER:
        logger.critical("Unable to start system as MQTT broker not defined")
        sys.exit(0)

    try:
        global mqtt_client
        mqtt_client = initialise_mqtt_client(mqtt.Client(client_id=MQTT_CLIENTID))
        initialise_obd()

        mqtt_client.loop_forever()

    except KeyboardInterrupt:
        exit_gracefully()

    logger.info("Session ended")



mqtt_client = None

if __name__ == '__main__':
    main()
