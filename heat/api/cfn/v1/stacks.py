# vim: tabstop=4 shiftwidth=4 softtabstop=4

#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Stack endpoint for Heat CloudFormation v1 API.
"""

import json
import socket

from heat.api.aws import exception
from heat.api.aws import utils as api_utils
from heat.common import wsgi
from heat.common import exception as heat_exception
from heat.rpc import client as rpc_client
from heat.common import template_format
from heat.rpc import api as engine_api
from heat.common import identifier
from heat.common import urlfetch
from heat.common import policy

from heat.openstack.common import log as logging
from heat.openstack.common.gettextutils import _

logger = logging.getLogger(__name__)


class StackController(object):

    """
    WSGI controller for stacks resource in Heat CloudFormation v1 API
    Implements the API actions
    """

    def __init__(self, options):
        self.options = options
        self.engine_rpcapi = rpc_client.EngineClient()
        self.policy = policy.Enforcer(scope='cloudformation')

    def _enforce(self, req, action):
        """Authorize an action against the policy.json."""
        try:
            self.policy.enforce(req.context, action, {})
        except heat_exception.Forbidden:
            raise exception.HeatAccessDeniedError("Action %s not allowed " %
                                                  action + "for user")
        except Exception as ex:
            # We expect policy.enforce to either pass or raise Forbidden
            # however, if anything else happens, we want to raise
            # HeatInternalFailureError, failure to do this results in
            # the user getting a big stacktrace spew as an API response
            raise exception.HeatInternalFailureError("Error authorizing " +
                                                     "action %s" % action)

    @staticmethod
    def _id_format(resp):
        """
        Format the StackId field in the response as an ARN, and process other
        IDs into the correct format.
        """
        if 'StackId' in resp:
            identity = identifier.HeatIdentifier(**resp['StackId'])
            resp['StackId'] = identity.arn()
        if 'EventId' in resp:
            identity = identifier.EventIdentifier(**resp['EventId'])
            resp['EventId'] = identity.event_id
        return resp

    @staticmethod
    def _extract_user_params(params):
        """
        Extract a dictionary of user input parameters for the stack

        In the AWS API parameters, each user parameter appears as two key-value
        pairs with keys of the form below:

        Parameters.member.1.ParameterKey
        Parameters.member.1.ParameterValue
        """
        return api_utils.extract_param_pairs(params,
                                             prefix='Parameters',
                                             keyname='ParameterKey',
                                             valuename='ParameterValue')

    def _get_identity(self, con, stack_name):
        """
        Generate a stack identifier from the given stack name or ARN.

        In the case of a stack name, the identifier will be looked up in the
        engine over RPC.
        """
        try:
            return dict(identifier.HeatIdentifier.from_arn(stack_name))
        except ValueError:
            return self.engine_rpcapi.identify_stack(con, stack_name)

    def list(self, req):
        """
        Implements ListStacks API action
        Lists summary information for all stacks
        """
        self._enforce(req, 'ListStacks')

        def format_stack_summary(s):
            """
            Reformat engine output into the AWS "StackSummary" format
            """
            # Map the engine-api format to the AWS StackSummary datatype
            keymap = {
                engine_api.STACK_CREATION_TIME: 'CreationTime',
                engine_api.STACK_UPDATED_TIME: 'LastUpdatedTime',
                engine_api.STACK_ID: 'StackId',
                engine_api.STACK_NAME: 'StackName',
                engine_api.STACK_STATUS_DATA: 'StackStatusReason',
                engine_api.STACK_TMPL_DESCRIPTION: 'TemplateDescription',
            }

            result = api_utils.reformat_dict_keys(keymap, s)

            action = s[engine_api.STACK_ACTION]
            status = s[engine_api.STACK_STATUS]
            result['StackStatus'] = '_'.join((action, status))

            # AWS docs indicate DeletionTime is ommitted for current stacks
            # This is still TODO(unknown) in the engine, we don't keep data for
            # stacks after they are deleted
            if engine_api.STACK_DELETION_TIME in s:
                result['DeletionTime'] = s[engine_api.STACK_DELETION_TIME]

            return self._id_format(result)

        con = req.context
        try:
            stack_list = self.engine_rpcapi.list_stacks(con)
        except Exception as ex:
            return exception.map_remote_error(ex)

        res = {'StackSummaries': [format_stack_summary(s) for s in stack_list]}

        return api_utils.format_response('ListStacks', res)

    def describe(self, req):
        """
        Implements DescribeStacks API action
        Gets detailed information for a stack (or all stacks)
        """
        self._enforce(req, 'DescribeStacks')

        def format_stack_outputs(o):
            keymap = {
                engine_api.OUTPUT_DESCRIPTION: 'Description',
                engine_api.OUTPUT_KEY: 'OutputKey',
                engine_api.OUTPUT_VALUE: 'OutputValue',
            }

            def replacecolon(d):
                return dict(map(lambda (k, v): (k.replace(':', '.'), v),
                                d.items()))

            def transform(attrs):
                """
                Recursively replace all : with . in dict keys
                so that they are not interpreted as xml namespaces.
                """
                new = replacecolon(attrs)
                for key, value in new.items():
                    if isinstance(value, dict):
                        new[key] = transform(value)
                return new

            return api_utils.reformat_dict_keys(keymap, transform(o))

        def format_stack(s):
            """
            Reformat engine output into the AWS "StackSummary" format
            """
            keymap = {
                engine_api.STACK_CAPABILITIES: 'Capabilities',
                engine_api.STACK_CREATION_TIME: 'CreationTime',
                engine_api.STACK_DESCRIPTION: 'Description',
                engine_api.STACK_DISABLE_ROLLBACK: 'DisableRollback',
                engine_api.STACK_UPDATED_TIME: 'LastUpdatedTime',
                engine_api.STACK_NOTIFICATION_TOPICS: 'NotificationARNs',
                engine_api.STACK_PARAMETERS: 'Parameters',
                engine_api.STACK_ID: 'StackId',
                engine_api.STACK_NAME: 'StackName',
                engine_api.STACK_STATUS_DATA: 'StackStatusReason',
                engine_api.STACK_TIMEOUT: 'TimeoutInMinutes',
            }

            result = api_utils.reformat_dict_keys(keymap, s)

            action = s[engine_api.STACK_ACTION]
            status = s[engine_api.STACK_STATUS]
            result['StackStatus'] = '_'.join((action, status))

            # Reformat outputs, these are handled separately as they are
            # only present in the engine output for a completely created
            # stack
            result['Outputs'] = []
            if engine_api.STACK_OUTPUTS in s:
                for o in s[engine_api.STACK_OUTPUTS]:
                    result['Outputs'].append(format_stack_outputs(o))

            # Reformat Parameters dict-of-dict into AWS API format
            # This is a list-of-dict with nasty "ParameterKey" : key
            # "ParameterValue" : value format.
            result['Parameters'] = [{'ParameterKey': k,
                                    'ParameterValue': v}
                                    for (k, v) in result['Parameters'].items()]

            return self._id_format(result)

        con = req.context
        # If no StackName parameter is passed, we pass None into the engine
        # this returns results for all stacks (visible to this user), which
        # is the behavior described in the AWS DescribeStacks API docs
        try:
            if 'StackName' in req.params:
                identity = self._get_identity(con, req.params['StackName'])
            else:
                identity = None

            stack_list = self.engine_rpcapi.show_stack(con, identity)

        except Exception as ex:
            return exception.map_remote_error(ex)

        res = {'Stacks': [format_stack(s) for s in stack_list]}

        return api_utils.format_response('DescribeStacks', res)

    def _get_template(self, req):
        """
        Get template file contents, either from local file or URL
        """
        if 'TemplateBody' in req.params:
            logger.debug('TemplateBody ...')
            return req.params['TemplateBody']
        elif 'TemplateUrl' in req.params:
            url = req.params['TemplateUrl']
            logger.debug('TemplateUrl %s' % url)
            try:
                return urlfetch.get(url)
            except IOError as exc:
                msg = _('Failed to fetch template: %s') % str(exc)
                raise exception.HeatInvalidParameterValueError(detail=msg)

        return None

    CREATE_OR_UPDATE_ACTION = (
        CREATE_STACK, UPDATE_STACK,
    ) = (
        "CreateStack", "UpdateStack",
    )

    def create(self, req):
        self._enforce(req, 'CreateStack')
        return self.create_or_update(req, self.CREATE_STACK)

    def update(self, req):
        self._enforce(req, 'UpdateStack')
        return self.create_or_update(req, self.UPDATE_STACK)

    def create_or_update(self, req, action=None):
        """
        Implements CreateStack and UpdateStack API actions
        Create or update stack as defined in template file
        """
        def extract_args(params):
            """
            Extract request parameters/arguments and reformat them to match
            the engine API.  FIXME: we currently only support a subset of
            the AWS defined parameters (both here and in the engine)
            """
            # TODO(shardy) : Capabilities, NotificationARNs
            keymap = {'TimeoutInMinutes': engine_api.PARAM_TIMEOUT,
                      'DisableRollback': engine_api.PARAM_DISABLE_ROLLBACK}

            if 'DisableRollback' in params and 'OnFailure' in params:
                msg = _('DisableRollback and OnFailure '
                        'may not be used together')
                raise exception.HeatInvalidParameterCombinationError(
                    detail=msg)

            result = {}
            for k in keymap:
                if k in params:
                    result[keymap[k]] = params[k]

            if 'OnFailure' in params:
                value = params['OnFailure']
                if value == 'DO_NOTHING':
                    result[engine_api.PARAM_DISABLE_ROLLBACK] = 'true'
                elif value in ('ROLLBACK', 'DELETE'):
                    result[engine_api.PARAM_DISABLE_ROLLBACK] = 'false'

            return result

        if action not in self.CREATE_OR_UPDATE_ACTION:
            msg = _("Unexpected action %(action)s") % ({'action': action})
            # This should not happen, so return HeatInternalFailureError
            return exception.HeatInternalFailureError(detail=msg)

        engine_action = {self.CREATE_STACK: self.engine_rpcapi.create_stack,
                         self.UPDATE_STACK: self.engine_rpcapi.update_stack}

        con = req.context

        # Extract the stack input parameters
        stack_parms = self._extract_user_params(req.params)

        # Extract any additional arguments ("Request Parameters")
        create_args = extract_args(req.params)

        try:
            templ = self._get_template(req)
        except socket.gaierror:
            msg = _('Invalid Template URL')
            return exception.HeatInvalidParameterValueError(detail=msg)

        if templ is None:
            msg = _("TemplateBody or TemplateUrl were not given.")
            return exception.HeatMissingParameterError(detail=msg)

        try:
            stack = template_format.parse(templ)
        except ValueError:
            msg = _("The Template must be a JSON or YAML document.")
            return exception.HeatInvalidParameterValueError(detail=msg)

        args = {'template': stack,
                'params': stack_parms,
                'files': {},
                'args': create_args}
        try:
            stack_name = req.params['StackName']
            if action == self.CREATE_STACK:
                args['stack_name'] = stack_name
            else:
                args['stack_identity'] = self._get_identity(con, stack_name)

            result = engine_action[action](con, **args)
        except Exception as ex:
            return exception.map_remote_error(ex)

        try:
            identity = identifier.HeatIdentifier(**result)
        except (ValueError, TypeError):
            response = result
        else:
            response = {'StackId': identity.arn()}

        return api_utils.format_response(action, response)

    def get_template(self, req):
        """
        Implements the GetTemplate API action
        Get the template body for an existing stack
        """
        self._enforce(req, 'GetTemplate')

        con = req.context
        try:
            identity = self._get_identity(con, req.params['StackName'])
            templ = self.engine_rpcapi.get_template(con, identity)
        except Exception as ex:
            return exception.map_remote_error(ex)

        if templ is None:
            msg = _('stack not not found')
            return exception.HeatInvalidParameterValueError(detail=msg)

        return api_utils.format_response('GetTemplate',
                                         {'TemplateBody': templ})

    def estimate_template_cost(self, req):
        """
        Implements the EstimateTemplateCost API action
        Get the estimated monthly cost of a template
        """
        self._enforce(req, 'EstimateTemplateCost')

        return api_utils.format_response('EstimateTemplateCost',
                                         {'Url':
                                          'http://en.wikipedia.org/wiki/Gratis'
                                          }
                                         )

    def validate_template(self, req):
        """
        Implements the ValidateTemplate API action
        Validates the specified template
        """
        self._enforce(req, 'ValidateTemplate')

        con = req.context
        try:
            templ = self._get_template(req)
        except socket.gaierror:
            msg = _('Invalid Template URL')
            return exception.HeatInvalidParameterValueError(detail=msg)
        if templ is None:
            msg = _("TemplateBody or TemplateUrl were not given.")
            return exception.HeatMissingParameterError(detail=msg)

        try:
            template = template_format.parse(templ)
        except ValueError:
            msg = _("The Template must be a JSON or YAML document.")
            return exception.HeatInvalidParameterValueError(detail=msg)

        logger.info('validate_template')

        def format_validate_parameter(key, value):
            """
            Reformat engine output into the AWS "ValidateTemplate" format
            """

            return {
                'ParameterKey': key,
                'DefaultValue': value.get(engine_api.PARAM_DEFAULT, ''),
                'Description': value.get(engine_api.PARAM_DESCRIPTION, ''),
                'NoEcho': value.get(engine_api.PARAM_NO_ECHO, 'false')
            }

        try:
            res = self.engine_rpcapi.validate_template(con, template)
            if 'Error' in res:
                return api_utils.format_response('ValidateTemplate',
                                                 res['Error'])

            res['Parameters'] = [format_validate_parameter(k, v)
                                 for k, v in res['Parameters'].items()]
            return api_utils.format_response('ValidateTemplate', res)
        except Exception as ex:
            return exception.map_remote_error(ex)

    def delete(self, req):
        """
        Implements the DeleteStack API action
        Deletes the specified stack
        """
        self._enforce(req, 'DeleteStack')

        con = req.context
        try:
            identity = self._get_identity(con, req.params['StackName'])
            res = self.engine_rpcapi.delete_stack(con, identity, cast=False)

        except Exception as ex:
            return exception.map_remote_error(ex)

        if res is None:
            return api_utils.format_response('DeleteStack', '')
        else:
            return api_utils.format_response('DeleteStack', res['Error'])

    def events_list(self, req):
        """
        Implements the DescribeStackEvents API action
        Returns events related to a specified stack (or all stacks)
        """
        self._enforce(req, 'DescribeStackEvents')

        def format_stack_event(e):
            """
            Reformat engine output into the AWS "StackEvent" format
            """
            keymap = {
                engine_api.EVENT_ID: 'EventId',
                engine_api.EVENT_RES_NAME: 'LogicalResourceId',
                engine_api.EVENT_RES_PHYSICAL_ID: 'PhysicalResourceId',
                engine_api.EVENT_RES_PROPERTIES: 'ResourceProperties',
                engine_api.EVENT_RES_STATUS_DATA: 'ResourceStatusReason',
                engine_api.EVENT_RES_TYPE: 'ResourceType',
                engine_api.EVENT_STACK_ID: 'StackId',
                engine_api.EVENT_STACK_NAME: 'StackName',
                engine_api.EVENT_TIMESTAMP: 'Timestamp',
            }

            result = api_utils.reformat_dict_keys(keymap, e)
            action = e[engine_api.EVENT_RES_ACTION]
            status = e[engine_api.EVENT_RES_STATUS]
            result['ResourceStatus'] = '_'.join((action, status))
            result['ResourceProperties'] = json.dumps(result[
                                                      'ResourceProperties'])

            return self._id_format(result)

        con = req.context
        stack_name = req.params.get('StackName', None)
        try:
            identity = stack_name and self._get_identity(con, stack_name)
            events = self.engine_rpcapi.list_events(con, identity)
        except Exception as ex:
            return exception.map_remote_error(ex)

        result = [format_stack_event(e) for e in events]

        return api_utils.format_response('DescribeStackEvents',
                                         {'StackEvents': result})

    @staticmethod
    def _resource_status(res):
        action = res[engine_api.RES_ACTION]
        status = res[engine_api.RES_STATUS]
        return '_'.join((action, status))

    def describe_stack_resource(self, req):
        """
        Implements the DescribeStackResource API action
        Return the details of the given resource belonging to the given stack.
        """
        self._enforce(req, 'DescribeStackResource')

        def format_resource_detail(r):
            """
            Reformat engine output into the AWS "StackResourceDetail" format
            """
            keymap = {
                engine_api.RES_DESCRIPTION: 'Description',
                engine_api.RES_UPDATED_TIME: 'LastUpdatedTimestamp',
                engine_api.RES_NAME: 'LogicalResourceId',
                engine_api.RES_METADATA: 'Metadata',
                engine_api.RES_PHYSICAL_ID: 'PhysicalResourceId',
                engine_api.RES_STATUS_DATA: 'ResourceStatusReason',
                engine_api.RES_TYPE: 'ResourceType',
                engine_api.RES_STACK_ID: 'StackId',
                engine_api.RES_STACK_NAME: 'StackName',
            }

            result = api_utils.reformat_dict_keys(keymap, r)

            result['ResourceStatus'] = self._resource_status(r)

            return self._id_format(result)

        con = req.context

        try:
            identity = self._get_identity(con, req.params['StackName'])
            resource_details = self.engine_rpcapi.describe_stack_resource(
                con,
                stack_identity=identity,
                resource_name=req.params.get('LogicalResourceId'))

        except Exception as ex:
            return exception.map_remote_error(ex)

        result = format_resource_detail(resource_details)

        return api_utils.format_response('DescribeStackResource',
                                         {'StackResourceDetail': result})

    def describe_stack_resources(self, req):
        """
        Implements the DescribeStackResources API action
        Return details of resources specified by the parameters.

        `StackName`: returns all resources belonging to the stack
        `PhysicalResourceId`: returns all resources belonging to the stack this
                              resource is associated with.

        Only one of the parameters may be specified.

        Optional parameter:

        `LogicalResourceId`: filter the resources list by the logical resource
        id.
        """
        self._enforce(req, 'DescribeStackResources')

        def format_stack_resource(r):
            """
            Reformat engine output into the AWS "StackResource" format
            """
            keymap = {
                engine_api.RES_DESCRIPTION: 'Description',
                engine_api.RES_NAME: 'LogicalResourceId',
                engine_api.RES_PHYSICAL_ID: 'PhysicalResourceId',
                engine_api.RES_STATUS_DATA: 'ResourceStatusReason',
                engine_api.RES_TYPE: 'ResourceType',
                engine_api.RES_STACK_ID: 'StackId',
                engine_api.RES_STACK_NAME: 'StackName',
                engine_api.RES_UPDATED_TIME: 'Timestamp',
            }

            result = api_utils.reformat_dict_keys(keymap, r)

            result['ResourceStatus'] = self._resource_status(r)

            return self._id_format(result)

        con = req.context
        stack_name = req.params.get('StackName')
        physical_resource_id = req.params.get('PhysicalResourceId')
        if stack_name and physical_resource_id:
            msg = 'Use `StackName` or `PhysicalResourceId` but not both'
            return exception.HeatInvalidParameterCombinationError(detail=msg)

        try:
            if stack_name is not None:
                identity = self._get_identity(con, stack_name)
            else:
                identity = self.engine_rpcapi.find_physical_resource(
                    con,
                    physical_resource_id=physical_resource_id)
            resources = self.engine_rpcapi.describe_stack_resources(
                con,
                stack_identity=identity,
                resource_name=req.params.get('LogicalResourceId'))

        except Exception as ex:
            return exception.map_remote_error(ex)

        result = [format_stack_resource(r) for r in resources]

        return api_utils.format_response('DescribeStackResources',
                                         {'StackResources': result})

    def list_stack_resources(self, req):
        """
        Implements the ListStackResources API action
        Return summary of the resources belonging to the specified stack.
        """
        self._enforce(req, 'ListStackResources')

        def format_resource_summary(r):
            """
            Reformat engine output into the AWS "StackResourceSummary" format
            """
            keymap = {
                engine_api.RES_UPDATED_TIME: 'LastUpdatedTimestamp',
                engine_api.RES_NAME: 'LogicalResourceId',
                engine_api.RES_PHYSICAL_ID: 'PhysicalResourceId',
                engine_api.RES_STATUS_DATA: 'ResourceStatusReason',
                engine_api.RES_TYPE: 'ResourceType',
            }

            result = api_utils.reformat_dict_keys(keymap, r)

            result['ResourceStatus'] = self._resource_status(r)

            return result

        con = req.context

        try:
            identity = self._get_identity(con, req.params['StackName'])
            resources = self.engine_rpcapi.list_stack_resources(
                con,
                stack_identity=identity)
        except Exception as ex:
            return exception.map_remote_error(ex)

        summaries = [format_resource_summary(r) for r in resources]

        return api_utils.format_response('ListStackResources',
                                         {'StackResourceSummaries': summaries})


def create_resource(options):
    """
    Stacks resource factory method.
    """
    deserializer = wsgi.JSONRequestDeserializer()
    return wsgi.Resource(StackController(options), deserializer)
