# File: fireeyeax_connector.py
#
# Copyright (c) Robert Drouin, 2021-2025
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software distributed under
# the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND,
# either express or implied. See the License for the specific language governing permissions
# and limitations under the License.
#
#
# Standard library imports
import json
import os
import sys
import uuid

# Phantom App imports
import phantom.app as phantom
import phantom.rules as phantom_rules
import requests
from bs4 import BeautifulSoup
from phantom.action_result import ActionResult
from phantom.base_connector import BaseConnector
from phantom.vault import Vault

# Usage of the consts file is recommended
from fireeyeax_consts import *


class RetVal(tuple):
    def __new__(cls, val1, val2=None):
        return tuple.__new__(RetVal, (val1, val2))


class FireeyeAxConnector(BaseConnector):
    def __init__(self):
        # Call the BaseConnectors init first
        super().__init__()

        self._state = None

        # Variable to hold a base_url in case the app makes REST calls
        # Do note that the app json defines the asset config, so please
        # modify this as you deem fit.
        self._base_url = None

    def _validate_integer(self, action_result, parameter, key):
        if parameter is not None:
            try:
                if not float(parameter).is_integer():
                    return action_result.set_status(phantom.APP_ERROR, VALID_INTEGER_MSG.format(key=key)), None

                parameter = int(parameter)
            except:
                return action_result.set_status(phantom.APP_ERROR, VALID_INTEGER_MSG.format(key=key)), None

            if parameter < 0:
                return action_result.set_status(phantom.APP_ERROR, NON_NEGATIVE_INTEGER_MSG.format(key=key)), None

        return phantom.APP_SUCCESS, parameter

    def _get_error_message_from_exception(self, e):
        """Get an appropriate error message from the exception.

        :param e: Exception object
        :return: error message
        """
        err_code = None
        err_message = ERR_MSG_UNAVAILABLE

        self.error_print("Error occurred.", e)
        try:
            if hasattr(e, "args"):
                if len(e.args) > 1:
                    err_code = e.args[0]
                    err_message = e.args[1]
                elif len(e.args) == 1:
                    err_message = e.args[0]
        except Exception as e:
            self.error_print(f"Error occurred while fetching exception information. Details: {e!s}")

        if not err_code:
            error_text = f"Error message: {err_message}"
        else:
            error_text = f"Error code: {err_code}. Error message: {err_message}"

        return error_text

    def _process_empty_response(self, response, action_result):
        if response.status_code == 200:
            return RetVal(phantom.APP_SUCCESS, {})

        return RetVal(
            action_result.set_status(phantom.APP_ERROR, f"Status code: {response.status_code}. Empty response and no information in the header"),
            None,
        )

    def _process_html_response(self, response, action_result):
        # An html response, treat it like an error
        status_code = response.status_code

        try:
            soup = BeautifulSoup(response.text, "html.parser")
            # Remove the script, style, footer and navigation part from the HTML message
            for element in soup(["script", "style", "footer", "nav"]):
                element.extract()
            error_text = soup.text
            split_lines = error_text.split("\n")
            split_lines = [x.strip() for x in split_lines if x.strip()]
            error_text = "\n".join(split_lines)
        except:
            error_text = "Cannot parse error details"

        message = f"Status Code: {status_code}. Data from server:\n{error_text}\n"

        message = message.replace("{", "{{").replace("}", "}}")
        return RetVal(action_result.set_status(phantom.APP_ERROR, message), None)

    def _process_json_response(self, r, action_result):
        # Try a json parse
        try:
            resp_json = r.json()
        except Exception as e:
            err = self._get_error_message_from_exception(e)
            return RetVal(action_result.set_status(phantom.APP_ERROR, f"Unable to parse JSON response. {err}"), None)

        # Please specify the status codes here
        if 200 <= r.status_code < 399:
            return RetVal(phantom.APP_SUCCESS, resp_json)

        # You should process the error returned in the json
        error_message = r.text.replace("{", "{{").replace("}", "}}")
        message = f"Error from server. Status Code: {r.status_code} Data from server: {error_message}"

        return RetVal(action_result.set_status(phantom.APP_ERROR, message), None)

    def _process_octet_response(self, r, action_result):
        # Create a unique ID for this file
        guid = uuid.uuid4()

        if hasattr(Vault, "get_vault_tmp_dir"):
            local_dir = f"{Vault.get_vault_tmp_dir()}/{guid}"
        else:
            local_dir = f"/opt/phantom/vault/tmp/{guid}"

        self.save_progress(f"Using temp directory: {guid}")

        try:
            os.makedirs(local_dir)
        except Exception as e:
            err = self._get_error_message_from_exception(e)
            return action_result.set_status(phantom.APP_ERROR, f"Unable to create temporary vault folder. {err}")

        action_params = self.get_current_param()

        # Get the parameter passed into the function that caused an octet-stream response
        # Many cases this will be a file download function
        acq_id = action_params.get("uuid", "no_id")

        # Set the file name for the vault
        filename = f"{acq_id}_artifacts.zip"

        zip_file_path = f"{local_dir}/{filename}"

        if r.status_code == 200:
            try:
                # Write the file to disk
                with open(zip_file_path, "wb") as f:
                    f.write(r.content)
            except Exception as e:
                err = self._get_error_message_from_exception(e)
                return RetVal(action_result.set_status(phantom.APP_ERROR, f"Unable to write zip file to disk. {err}"), None)
            else:
                try:
                    vault_results = Vault.add_attachment(zip_file_path, self.get_container_id(), file_name=filename)
                    return RetVal(phantom.APP_SUCCESS, vault_results)
                except Exception as e:
                    err = self._get_error_message_from_exception(e)
                    return RetVal(action_result.set_status(phantom.APP_ERROR, f"Unable to store file in Phantom Vault. {err}"), None)

        message = "Error from server. Status Code: {} Data from server: {}".format(r.status_code, r.text.replace("{", "{{").replace("}", "}}"))

        return RetVal(action_result.set_status(phantom.APP_ERROR, message), None)

    def _process_response(self, r, action_result):
        # store the r_text in debug data, it will get dumped in the logs if the action fails
        if hasattr(action_result, "add_debug_data"):
            action_result.add_debug_data({"r_status_code": r.status_code})
            action_result.add_debug_data({"r_text": r.text})
            action_result.add_debug_data({"r_headers": r.headers})

        # Process each 'Content-Type' of response separately

        # Process an octet response.
        # This is mainly for processing data downloaded during acquisition.
        if "octet" in r.headers.get("Content-Type", ""):
            return self._process_octet_response(r, action_result)

        # Process a json response
        if "json" in r.headers.get("Content-Type", ""):
            return self._process_json_response(r, action_result)

        # Process an HTML response, Do this no matter what the api talks.
        # There is a high chance of a PROXY in between phantom and the rest of
        # world, in case of errors, PROXY's return HTML, this function parses
        # the error and adds it to the action_result.
        if "html" in r.headers.get("Content-Type", ""):
            return self._process_html_response(r, action_result)

        # it's not content-type that is to be parsed, handle an empty response
        if not r.text:
            return self._process_empty_response(r, action_result)

        # everything else is actually an error at this point
        message = "Can't process response from server. Status Code: {} Data from server: {}".format(
            r.status_code, r.text.replace("{", "{{").replace("}", "}}")
        )

        return RetVal(action_result.set_status(phantom.APP_ERROR, message), None)

    def _make_rest_call(self, endpoint, action_result, method="get", get_file=False, **kwargs):
        # **kwargs can be any additional parameters that requests.request accepts
        resp_json = None

        try:
            request_func = getattr(requests, "post")
        except AttributeError:
            return RetVal(action_result.set_status(phantom.APP_ERROR, f"Invalid method: {method}"), resp_json)

        try:
            login_url = f"{self._base_url}{FIREEYEAX_LOGIN_ENDPOINT}"

            self.save_progress("AX Auth: Execute REST Call")

            req = request_func(
                login_url,
                auth=(self._username, self._password),  # basic authentication
                verify=self._verify,
                headers=self._header,
            )
            # Add the authorization value to the header
            if req.status_code >= 200 and req.status_code <= 204:
                self.save_progress("AX Auth: Process Response - Token Success")

                self._header["X-FeApi-Token"] = req.headers.get("X-FeApi-Token")
            else:
                self.save_progress("AX Auth: Process Response - Token Failed")

                message = "AX Auth Failed, please confirm 'username' and 'password'"

                return RetVal(action_result.set_status(phantom.APP_ERROR, message), None)

        except requests.exceptions.InvalidURL:
            error_message = f"Error connecting to server. Invalid URL {login_url}"
            return RetVal(action_result.set_status(phantom.APP_ERROR, error_message), resp_json)
        except requests.exceptions.ConnectionError:
            error_message = f"Error connecting to server. Connection Refused from the Server for {login_url}"
            return RetVal(action_result.set_status(phantom.APP_ERROR, error_message), resp_json)
        except requests.exceptions.InvalidSchema:
            error_message = f"Error connecting to server. No connection adapters were found for {login_url}"
            return RetVal(action_result.set_status(phantom.APP_ERROR, error_message), resp_json)
        except Exception as e:
            err = self._get_error_message_from_exception(e)
            return RetVal(action_result.set_status(phantom.APP_ERROR, f"Error Connecting to server. {err}"), resp_json)
        else:
            # After we Login now proceed to call the endpoint we want
            try:
                request_func = getattr(requests, method)
            except AttributeError:
                return RetVal(action_result.set_status(phantom.APP_ERROR, f"Invalid method: {method}"), resp_json)

            # Create a URL to connect to
            try:
                url = f"{self._base_url}{endpoint}"
            except:
                err_msg = "Failed to parse the url"
                return RetVal(action_result.set_status(phantom.APP_ERROR, err_msg), resp_json)

            # If we are submitting a file for detonation we need to update the content-type
            if "files" in list(kwargs.keys()) or FIREEYEAX_DETONATE_FILE_ENDPOINT == endpoint:
                # Remove the Content-Type variable. Requests adds this automatically when uploading Files
                del self._header["Content-Type"]
            # If we are downloading the artifact data from a submissions we need to update the content-type
            elif get_file:
                self._header["Content-Type"] = "application/zip"

            try:
                r = request_func(url, verify=self._verify, headers=self._header, **kwargs)
            except Exception as e:
                err = self._get_error_message_from_exception(e)
                return RetVal(action_result.set_status(phantom.APP_ERROR, f"Error Connecting to server. {err}"), resp_json)

            else:
                # Logout of the API.

                # Force reset of the header content-type
                # Have to do this since detonate file makes up change the value
                # Probably a better way to do this
                self._header["Content-Type"] = "application/json"

                try:
                    self.save_progress("AX Logout: Execute REST Call")

                    logout_url = f"{self._base_url}{FIREEYEAX_LOGOUT_ENDPOINT}"

                    self.save_progress("AX Auth: Execute REST Call")

                    req = requests.post(logout_url, verify=self._verify, headers=self._header)

                except requests.exceptions.RequestException as e:
                    err = self._get_error_message_from_exception(e)
                    return RetVal(action_result.set_status(phantom.APP_ERROR, f"Error Connecting to server. {err}"), resp_json)

        return self._process_response(r, action_result)

    def _handle_test_connectivity(self, param):
        # Add an action result object to self (BaseConnector) to represent the action for this param
        action_result = self.add_action_result(ActionResult(dict(param)))

        self.save_progress("Connecting to endpoint")

        params = {"duration": "2_hours"}

        endpoint = FIREEYEAX_ALERTS_ENDPOINT

        # make rest call
        ret_val, _ = self._make_rest_call(endpoint, action_result, params=params)

        if phantom.is_fail(ret_val):
            self.save_progress("Test Connectivity Failed")
            return action_result.get_status()

        # Return success
        self.save_progress("Test Connectivity Passed")
        return action_result.set_status(phantom.APP_SUCCESS)

    def _handle_detonate_file(self, param):
        # Implement the handler here
        # use self.save_progress(...) to send progress messages back to the platform
        self.save_progress(f"In action handler for: {self.get_action_identifier()}")

        # Add an action result object to self (BaseConnector) to represent the action for this param
        action_result = self.add_action_result(ActionResult(dict(param)))

        # Access action parameters passed in the 'param' dictionary
        vault_id = param.get("vault_id")

        # Get vault info from the vauld_id parameter
        try:
            success, msg, vault_info = phantom_rules.vault_info(vault_id=vault_id)
        except:
            return action_result.set_status(phantom.APP_ERROR, "Error occurred while fetching the vault information of the specified Vault ID")

        if not vault_info:
            try:
                error_message = f"Error occurred while fetching the vault information of the Vault ID: {vault_id}"
            except:
                error_message = "Error occurred while fetching the vault information of the specified Vault ID"

            return action_result.set_status(phantom.APP_ERROR, error_message)

        # Loop through the Vault infomation
        for item in vault_info:
            vault_path = item.get("path")
            if vault_path is None:
                return action_result.set_status(phantom.APP_ERROR, "Could not find a path associated with the provided Vault ID")
            try:
                # Open the file
                vault_file = open(vault_path, "rb")
                # Create the files data to send to the console
                files = {"file": (item["name"], vault_file)}
            except Exception as e:
                error_message = self._get_error_message_from_exception(e)
                return action_result.set_status(phantom.APP_ERROR, f"Unable to open vault file: {error_message}")

        # Process parameters
        profile = param.get("profile")
        try:
            profile = [x.strip() for x in profile.split(",")]
        except Exception:
            return action_result.set_status(phantom.APP_ERROR, f"Error occurred while processing the {PROFILE_ACTION_PARAM}")
        profile = list([_f for _f in profile if _f])
        if not profile:
            return action_result.set_status(phantom.APP_ERROR, f"Please provide a valid value for the {PROFILE_ACTION_PARAM}")

        # Get the other parameters and information
        priority = 0 if param["priority"].lower() == "normal" else 1
        analysis_type = 1 if param["analysis_type"].lower() == "live" else 2

        timeout = param.get("timeout")
        # Validate 'timeout' action parameter
        ret_val, timeout = self._validate_integer(action_result, timeout, TIMEOUT_ACTION_PARAM)
        if phantom.is_fail(ret_val):
            return action_result.get_status()

        force = "true" if param.get("force", True) else "false"

        # When analysis type = 2 (Sandbox), prefetch must equal 1
        if analysis_type == 2:
            prefetch = 1
        else:
            prefetch = 1 if param.get("prefetch", False) else 0

        enable_vnc = "true" if param.get("enable_vnc", False) else "false"

        data = {}

        # Get the application code to use for the detonation
        application = self.get_application_code(param.get("application"))

        # Create the data based on the parameters
        options = {
            "priority": priority,
            "analysistype": analysis_type,
            "force": force,
            "prefetch": prefetch,
            "profiles": profile,
            "application": application,
            "timeout": timeout,
            "enable_vnc": enable_vnc,
        }

        # Need to stringify the options parameter
        data = {"options": json.dumps(options)}

        endpoint = FIREEYEAX_DETONATE_FILE_ENDPOINT

        # make rest call
        ret_val, response = self._make_rest_call(endpoint, action_result, method="post", files=files, data=data)

        if phantom.is_fail(ret_val):
            return action_result.get_status()

        # Now post process the data, uncomment code as you deem fit
        try:
            resp_data = response[0]
        except Exception as e:
            err = self._get_error_message_from_exception(e)
            return action_result.set_status(phantom.APP_ERROR, f"Error occurred while fetching data from API response. {err}")

        try:
            resp_data["submission_details"] = json.loads(resp_data["submission_details"])
        except Exception as e:
            err = self._get_error_message_from_exception(e)
            return action_result.set_status(phantom.APP_ERROR, f"Error occurred while processing API response. {err}")

        # Add the response into the data section
        if isinstance(resp_data, list):
            for alert in resp_data:
                action_result.add_data(alert)
        else:
            action_result.add_data(resp_data)

        return action_result.set_status(phantom.APP_SUCCESS)

    def _handle_detonate_url(self, param):
        # Implement the handler here
        # use self.save_progress(...) to send progress messages back to the platform
        self.save_progress(f"In action handler for: {self.get_action_identifier()}")

        # Add an action result object to self (BaseConnector) to represent the action for this param
        action_result = self.add_action_result(ActionResult(dict(param)))

        data = {}

        # Access action parameters passed in the 'param' dictionary
        urls = param.get("urls")
        try:
            urls = [x.strip() for x in urls.split(",")]
        except Exception:
            return action_result.set_status(phantom.APP_ERROR, f"Error occurred while processing the {URL_ACTION_PARAM}")
        urls = list([_f for _f in urls if _f])
        if not urls:
            return action_result.set_status(phantom.APP_ERROR, f"Please provide a valid value for the {URL_ACTION_PARAM}")

        profile = param.get("profile")
        try:
            profile = [x.strip() for x in profile.split(",")]
        except Exception:
            return action_result.set_status(phantom.APP_ERROR, f"Error occurred while processing the {PROFILE_ACTION_PARAM}")
        profile = list([_f for _f in profile if _f])
        if not profile:
            return action_result.set_status(phantom.APP_ERROR, f"Please provide a valid value for the {PROFILE_ACTION_PARAM}")

        # Get the other parameters and information
        priority = 0 if param["priority"].lower() == "normal" else 1
        analysis_type = 1 if param["analysis_type"].lower() == "live" else 2

        force = "true" if param.get("force", True) else "false"

        prefetch = 1 if param.get("prefetch", False) else 0

        timeout = param.get("timeout")
        # Validate 'timeout' action parameter
        ret_val, timeout = self._validate_integer(action_result, timeout, TIMEOUT_ACTION_PARAM)
        if phantom.is_fail(ret_val):
            return action_result.get_status()

        # Get the application code to use for the detonation
        application = self.get_application_code(param.get("application"))

        enable_vnc = "true" if param.get("enable_vnc", False) else "false"

        data = {
            "priority": priority,
            "analysistype": analysis_type,
            "force": force,
            "prefetch": prefetch,
            "urls": urls,
            "profiles": profile,
            "application": application,
            "enable_vnc": enable_vnc,
            "timeout": timeout,
        }

        endpoint = FIREEYEAX_DETONATE_URL_ENDPOINT

        # make rest call
        ret_val, response = self._make_rest_call(endpoint, action_result, method="post", data=json.dumps(data))

        if phantom.is_fail(ret_val):
            return action_result.get_status()

        # Now post process the data,  uncomment code as you deem fit

        # Add the response into the data section
        # Updating the response data so we can properly get the data.
        # The data is returned by a string so we need to convert it into JSON to be useable
        try:
            resp_data = response["entity"]["response"]
        except Exception as e:
            err = self._get_error_message_from_exception(e)
            return action_result.set_status(phantom.APP_ERROR, f"Error occurred while fetching data from API response. {err}")

        try:
            resp_data[0]["submission_details"] = json.loads(resp_data[0]["submission_details"])
        except Exception as e:
            err = self._get_error_message_from_exception(e)
            return action_result.set_status(phantom.APP_ERROR, f"Error occurred while processing API response. {err}")

        if isinstance(resp_data, list):
            for alert in resp_data:
                action_result.add_data(alert)
        else:
            action_result.add_data(resp_data)

        return action_result.set_status(phantom.APP_SUCCESS)

    def _handle_get_report(self, param):
        # Implement the handler here
        # use self.save_progress(...) to send progress messages back to the platform
        self.save_progress(f"In action handler for: {self.get_action_identifier()}")

        # Add an action result object to self (BaseConnector) to represent the action for this param
        action_result = self.add_action_result(ActionResult(dict(param)))

        # Access action parameters passed in the 'param' dictionary

        # Required values can be accessed directly
        id = param.get("id")

        params = {}
        # Add parameter to get more information on the report
        params["info_level"] = "extended" if param.get("extended", False) else "normal"

        endpoint = FIREEYEAX_GET_RESULTS_ENDPOINT.format(submission_id=id)

        # make rest call
        ret_val, response = self._make_rest_call(endpoint, action_result, params=params)

        if phantom.is_fail(ret_val):
            return action_result.get_status()

        # Add the response into the data section
        action_result.add_data(response)

        return action_result.set_status(phantom.APP_SUCCESS)

    def _handle_save_artifacts(self, param):
        # Implement the handler here
        # use self.save_progress(...) to send progress messages back to the platform
        self.save_progress(f"In action handler for: {self.get_action_identifier()}")

        # Add an action result object to self (BaseConnector) to represent the action for this param
        action_result = self.add_action_result(ActionResult(dict(param)))

        # Access action parameters passed in the 'param' dictionary
        # Required values can be accessed directly
        uuid = param.get("uuid")

        endpoint = FIREEYEAX_SAVE_ARTIFACTS_ENDPOINT.format(uuid=uuid)

        # make rest call
        ret_val, response = self._make_rest_call(endpoint, action_result, get_file=True)

        if phantom.is_fail(ret_val):
            return action_result.get_status()

        # Add the response into the data section
        action_result.add_data(response)

        return action_result.set_status(phantom.APP_SUCCESS)

    def _handle_get_status(self, param):
        # Implement the handler here
        # use self.save_progress(...) to send progress messages back to the platform
        self.save_progress(f"In action handler for: {self.get_action_identifier()}")

        # Add an action result object to self (BaseConnector) to represent the action for this param
        action_result = self.add_action_result(ActionResult(dict(param)))

        # Access action parameters passed in the 'param' dictionary

        # Required values can be accessed directly
        id = param.get("id")

        endpoint = FIREEYEAX_GET_STATUS_ENDPOINT.format(submission_id=id)

        # make rest call
        ret_val, response = self._make_rest_call(endpoint, action_result)

        if phantom.is_fail(ret_val):
            return action_result.get_status()

        # Add the response into the data section
        resp_data = response
        try:
            resp_data["submission_details"] = json.loads(resp_data["submission_details"])
        except Exception as e:
            err = self._get_error_message_from_exception(e)
            return action_result.set_status(phantom.APP_ERROR, f"Error occurred while processing API response. {err}")

        action_result.add_data(response)

        return action_result.set_status(phantom.APP_SUCCESS)

    # Returns the application code for submitting URL's and Files to AX
    def get_application_code(self, application):
        # Set default
        code = "0"
        try:
            code = FIREEYEAX_APPLICATION_CODES[application]
        except KeyError:
            self.save_progress(
                "Could not find the specified application in the available application list. Reverting to Default application code 0"
            )
            pass

        return code

    def handle_action(self, param):
        """This function gets current action identifier and calls member function of its own to handle the action.
        :param param: dictionary which contains information about the actions to be executed
        :return: status success/failure
        """

        action_mapping = {
            "test_connectivity": self._handle_test_connectivity,
            "detonate_file": self._handle_detonate_file,
            "detonate_url": self._handle_detonate_url,
            "get_report": self._handle_get_report,
            "save_artifacts": self._handle_save_artifacts,
            "get_status": self._handle_get_status,
        }

        # Get the action that we are supposed to execute for this App Run
        action = self.get_action_identifier()
        action_execution_status = phantom.APP_SUCCESS

        if action in list(action_mapping.keys()):
            action_function = action_mapping[action]
            action_execution_status = action_function(param)
        return action_execution_status

    def initialize(self):
        # Load the state in initialize, use it to store data
        # that needs to be accessed across actions
        self._state = self.load_state()

        # get the asset config
        config = self.get_config()

        # Check to see which instance the user selected. Use the appropriate URL.
        base_url = config.get("base_url").strip("/")

        self._base_url = f"{base_url}/{FIREEYEAX_API_PATH}"

        self._header = {"Content-Type": "application/json", "Accept": "application/json"}

        self._username = config.get("username")
        self._password = config.get("password")

        self._verify = config.get("verify_server_cert", False)

        return phantom.APP_SUCCESS

    def finalize(self):
        # Save the state, this data is saved across actions and app upgrades
        self.save_state(self._state)
        return phantom.APP_SUCCESS


def main():
    # import pudb
    import argparse

    # pudb.set_trace()

    argparser = argparse.ArgumentParser()

    argparser.add_argument("input_test_json", help="Input Test JSON file")
    argparser.add_argument("-u", "--username", help="username", required=False)
    argparser.add_argument("-p", "--password", help="password", required=False)
    argparser.add_argument("-v", "--verify", action="store_true", help="verify", required=False, default=False)

    args = argparser.parse_args()
    session_id = None

    username = args.username
    password = args.password
    verify = args.verify

    if username is not None and password is None:
        # User specified a username but not a password, so ask
        import getpass

        password = getpass.getpass("Password: ")

    if username and password:
        try:
            login_url = FireeyeAxConnector._get_phantom_base_url() + "/login"

            print("Accessing the Login page")
            r = requests.get(login_url, verify=verify)
            csrftoken = r.cookies["csrftoken"]

            data = dict()
            data["username"] = username
            data["password"] = password
            data["csrfmiddlewaretoken"] = csrftoken

            headers = dict()
            headers["Cookie"] = "csrftoken=" + csrftoken
            headers["Referer"] = login_url

            print("Logging into Platform to get the session id")
            r2 = requests.post(login_url, verify=verify, data=data, headers=headers)
            session_id = r2.cookies["sessionid"]
        except Exception as e:
            print("Unable to get session id from the platform. Error: " + str(e))
            sys.exit(1)

    with open(args.input_test_json) as f:
        in_json = f.read()
        in_json = json.loads(in_json)
        print(json.dumps(in_json, indent=4))

        connector = FireeyeAxConnector()
        connector.print_progress_message = True

        if session_id is not None:
            in_json["user_session_token"] = session_id
            connector._set_csrf_info(csrftoken, headers["Referer"])

        ret_val = connector._handle_action(json.dumps(in_json), None)
        print(json.dumps(json.loads(ret_val), indent=4))

    sys.exit(0)


if __name__ == "__main__":
    main()
