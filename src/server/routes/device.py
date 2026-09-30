from flask import Blueprint, request
from device_commands import ConnectionManager
from utility import string_to_bool, sanitize, json_response
from tables.device import Device
import database
import wrappers

api_routes = Blueprint('device', __name__)


@api_routes.route('/api/v1/devices/<int:device_id>', methods=['GET'])
@wrappers.verify_login(public=True)
def get_device(device_id, **kwargs):
    device = database.get_devices(id=device_id)
    if device:
        return json_response(device.json())
    else:
        return json_response({'message': 'Device not found.'}, status=404)


@api_routes.route('/api/v1/devices/<int:device_id>', methods=['PUT'])
@wrappers.verify_login(public=True)
def update_device(device_id, **kwargs):
    name = request.json.get('name', None)
    connected = string_to_bool(request.json.get('connected', None))
    valid, message = Device.verify_fields(name=name)
    if not valid:
        return json_response({'message': message}, 400)
    device = database.edit_device(device_id, name=name, connected=connected)
    if device is None:
        return json_response({'message': 'Device not found.'}, 404)
    return json_response(device.json())


@api_routes.route('/api/v1/devices', methods=['GET'])
@wrappers.verify_login(public=True)
def get_devices(**kwargs):
    archived = string_to_bool(request.args.get('archived', None))
    connected = string_to_bool(request.args.get('connected', None))
    in_use = string_to_bool(request.args.get('inUse', None))
    is_pod = string_to_bool(request.args.get('isPod', None))
    devices = database.get_devices(
        archived=archived, connected=connected, in_use=in_use, is_pod=is_pod)
    return json_response([device.json() for device in devices])


@api_routes.route('/api/v1/devices', methods=['POST'])
@wrappers.verify_login(roles=wrappers.ADMIN_ROLES, public=True)
def add_device(**kwargs):
    mac_address = sanitize(request.json.get('macAddress', None))
    if not mac_address:
        return json_response({'message': 'Mac address must not be empty.'}, 400)
    valid, message = Device.verify_fields(mac_address=mac_address)
    if not valid:
        return json_response({'message': message}, 400)
    success, device = database.add_pod(mac_address)
    if success:
        return json_response(device.json())
    else:
        return json_response({'message': 'Pod "{0}" is already associated with this mac address.'.format(device.get_name())}, 400)


@api_routes.route('/api/v1/devices/<int:device_id>', methods=['DELETE'])
@wrappers.verify_login(roles=wrappers.ADMIN_ROLES, public=True)
def remove_device(device_id, **kwargs):
    success = database.delete_device(device_id)
    if success:
        return json_response()
    else:
        return json_response({'message': 'Failed to delete device.'}, status=400)

# ----------------------------
# The following paths are only
# possible for pod Devices.
# ----------------------------


@api_routes.route('/api/v1/devices/<int:device_id>/blink', methods=['POST'])
@wrappers.verify_login()
def blink(device_id, **kwargs):
    device = database.get_devices(id=device_id, is_pod=True, in_use=False)
    if device:
        command = {}
        command['cmd'] = 'blink'
        command['op'] = request.json.get('op', 'start')
        command['time'] = request.json.get('time', 15)
        ConnectionManager.instance.send_command(device_id, command)
        return json_response()
    else:
        return json_response({'message': 'Device not found.'}, 404)
