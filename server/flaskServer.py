import os
import ssl
import json
import sys

from flask import render_template, request, Response
from flask_security import login_required, auth_token_required, current_user, roles_accepted
from flask_security.utils import encrypt_password, verify_and_update_password

from core import config
from core.context import running_context
from core.helpers import locate_workflows_in_directory
from core import helpers
from . import forms, interface
from core.case.subscription import CaseSubscriptions, add_cases, delete_cases, rename_case
from core.options import Options
import core.case.database as case_database
import core.case.subscription as case_subscription
from . import database, appDevice
from .app import app
from .database import User
from .triggers import Triggers
from gevent import monkey
from server.appBlueprint import get_base_app_functions
from xml.etree import ElementTree

monkey.patch_all()

user_datastore = database.user_datastore

urls = ["/", "/key", "/workflow", "/configuration", "/interface", "/execution/listener", "/execution/listener/triggers",
        "/roles", "/users", "/configuration", '/cases', '/apps']

default_urls = urls
userRoles = database.userRoles
database.initialize_userRoles(urls)
db = database.db


# devClass = appDevice.Device()


# Creates Test Data
@app.before_first_request
def create_user():
    # db.drop_all()
    database.db.create_all()

    if not database.User.query.first():
        # Add Credentials to Splunk app
        # db.session.add(Device(name="deviceOne", app="splunk", username="admin", password="hello", ip="192.168.0.1", port="5000"))

        adminRole = user_datastore.create_role(name="admin", description="administrator", pages=default_urls)
        # userRole = user_datastore.create_role(name="user", description="user")

        u = user_datastore.create_user(email='admin', password=encrypt_password('admin'))
        # u2 = user_datastore.create_user(email='user', password=encrypt_password('user'))

        user_datastore.add_role_to_user(u, adminRole)

        database.db.session.commit()

    if database.db.session.query(appDevice.App).all() == []:
        # initialize app table
        path = os.path.abspath('apps/')
        for name in os.listdir(path):
            if (os.path.isdir(os.path.join(path, name))) and ('cache' not in name):
                database.db.session.add(appDevice.App(app=name, devices=[]))

"""
    URLS
"""

@app.route("/")
@login_required
def default():
    if current_user.is_authenticated:
        default_page_name = "dashboard"
        args = {"apps": running_context.getApps(), "authKey": current_user.get_auth_token(),
                "currentUser": current_user.email, "default_page": default_page_name}
        return render_template("container.html", **args)
    else:
        return {"status": "Could Not Log In."}


# Returns the API key for the user
@app.route('/key', methods=["GET", "POST"])
@login_required
def loginInfo():
    if current_user.is_authenticated:
        return json.dumps({"auth_token": current_user.get_auth_token()})
    else:
        return {"status": "Could Not Log In."}


@app.route('/apps/', methods=['GET'])
@auth_token_required
@roles_accepted(*userRoles["/apps"])
def list_all_apps():
    return json.dumps({"apps": helpers.list_apps()})


@app.route('/apps/actions', methods=['GET'])
@auth_token_required
@roles_accepted(*userRoles["/apps"])
def list_all_apps_and_actions():
    apps = helpers.list_apps()
    return json.dumps({app: list((set(helpers.list_app_functions(app)) - get_base_app_functions())) for app in apps})


@app.route("/workflows", methods=['GET'])
@auth_token_required
@roles_accepted(*userRoles["/workflow"])
def display_available_workflows():
    workflow_names = [helpers.get_workflow_name_from_file(os.path.join(config.workflowsPath, workflow))
                      for workflow in locate_workflows_in_directory()]
    return json.dumps({"workflows": workflow_names})


@app.route("/workflows/templates", methods=['GET'])
@auth_token_required
@roles_accepted(*userRoles["/workflow"])
def display_available_workflow_templates():
    template_names = [helpers.get_workflow_name_from_file(os.path.join(config.templatesPath, workflow))
                      for workflow in locate_workflows_in_directory(config.templatesPath)]
    return json.dumps({"templates": template_names})


@app.route("/workflow/<string:name>", methods=['GET'])
@auth_token_required
@roles_accepted(*userRoles["/workflow"])
def display_workflow(name):
    if name in running_context.controller.workflows:
        return json.dumps({"status": "success",
                           "steps": running_context.controller.workflows[name].get_cytoscape_data(),
                           'options': running_context.controller.workflows[name].options.as_json()})
    else:
        return json.dumps({"status": "error: name {0} not found".format(name)})



@app.route("/workflow/<string:name>/<string:action>", methods=['POST'])
@auth_token_required
@roles_accepted(*userRoles["/workflow"])
def workflow(name, action):
    if action == 'add':
        form = forms.AddPlayForm(request.form)
        if form.validate():
            status = 'success'
            template = form.template.data
            if template:
                if template in [os.path.splitext(workflow)[0]
                                for workflow in locate_workflows_in_directory(config.templatesPath)]:
                    running_context.controller.create_workflow_from_template(workflow_name=name,
                                                                             template_name=template)
                else:
                    running_context.controller.create_workflow_from_template(workflow_name=name)
                    status = 'warning: template not found. Using default template'
            else:
                running_context.controller.create_workflow_from_template(workflow_name=name)

            if name in running_context.controller.workflows:

                return json.dumps(
                    {'workflow': {'name': name,
                                  'steps': running_context.controller.workflows[name].get_cytoscape_data(),
                                  'options': running_context.controller.workflows[name].options.as_json()},
                     'status': status})
            else:
                json.dumps({'status': 'error: could not add workflow'})
        else:
            return json.dumps({'status': 'error: invalid form'})

    elif action == 'edit':
        if name in running_context.controller.workflows:
            form = forms.EditPlayNameForm(request.form)
            if form.validate():
                enabled = form.enabled.data if form.enabled.data else False
                scheduler = {'type': form.scheduler_type.data if form.scheduler_type.data else 'chron',
                             'autorun': str(form.autoRun.data).lower() if form.autoRun.data else 'false',
                             'args': json.loads(form.scheduler_args.data) if form.scheduler_args.data else {}}
                running_context.controller.workflows[name].options = Options(scheduler=scheduler, enabled=enabled)
                if form.new_name.data:
                    running_context.controller.updateWorkflowName(oldName=name, newName=form.new_name.data)
                    name = form.new_name.data

                return json.dumps(
                        {'workflow': {'name': name,
                                      'options': running_context.controller.workflows[name].options.as_json()},
                         'status': 'success'})
            else:
                return json.dumps({'status': 'error: invalid form'})
        else:
            return json.dumps({'status': 'error: workflow {0} is not valid'.format(name)})

    elif action == 'save':
        if name in running_context.controller.workflows:
            form = forms.SavePlayForm(request.form)
            if request.get_json():
                if 'cytoscape' in request.get_json():
                    running_context.controller.workflows[name].from_cytoscape_data(
                        json.loads(request.get_json()['cytoscape']))
                filename = name
                if 'filename' in request.get_json() and request.get_json()['filename']:
                    filename = request.get_json()['filename']
                try:
                    write_format = 'w' if sys.version_info[0] == 2 else 'wb'
                    workflow_filename = os.path.join(config.workflowsPath, '{0}.workflow'.format(filename))
                    with open(workflow_filename, write_format) as workflow_out:
                        xml = ElementTree.tostring(running_context.controller.workflows[name].to_xml())
                        workflow_out.write(xml)
                    return json.dumps(
                        {"status": "success",
                         "steps": running_context.controller.workflows[name].get_cytoscape_data()})
                except (OSError, IOError) as e:
                    return json.dumps(
                        {"status": "Error saving: {0}".format(e.message),
                         "steps": running_context.controller.workflows[name].get_cytoscape_data()})
            else:
                return json.dumps({"status": "malformed json!"})
        else:
            return json.dumps({'status': 'error: workflow {0} is not valid'.format(name)})

    elif action == 'delete':
        workflow_names = {workflow: helpers.get_workflow_name_from_file(os.path.join(config.workflowsPath, workflow))
                          for workflow in locate_workflows_in_directory()}
        # TODO: pass in filename to delete
        matching_workflows = [workflow_filename
                              for workflow_filename, workflow_name in workflow_names.items() if workflow_name == name]
        status = 'success'
        if matching_workflows:
            try:
                for workflow in matching_workflows:
                    os.remove(os.path.join(config.workflowsPath, workflow))
            except OSError:
                status = 'error deleting files'
        if name in running_context.controller.workflows:
            running_context.controller.removeWorkflow(name)
        return json.dumps({'status': status,
                           'workflows': list(running_context.controller.workflows.keys())})

    elif name in running_context.controller.workflows:
        if action == "cytoscape":
            output = running_context.controller.workflows[name].get_cytoscape_data()
            return json.dumps(output)
        if action == "execute":
            running_context.controller.executeWorkflow(name=name, start="start")
            responseFormat = request.form.get("format")

            response = json.dumps({"status": "success"})

            # if responseFormat == "cytoscape":
            #     #response = json.dumps(helpers.get_cytoscape_data(steps=steps))
            #     response = json.dumps({"status": "success"})
            # else:
            #     response = json.dumps(str(steps))
            return Response(response, mimetype="application/json")
    else:
        return json.dumps({"status": "error"})


@app.route('/cases', methods=['GET'])
@auth_token_required
@roles_accepted(*userRoles['/cases'])
def display_cases():
    return json.dumps(case_database.case_db.cases_as_json())


@app.route('/cases/<string:case_name>/<string:action>', methods=['POST'])
@auth_token_required
@roles_accepted(*userRoles['/cases'])
def crud_case(case_name, action):
    if action == 'add':
        case = CaseSubscriptions()
        add_cases({"{0}".format(str(case_name)): case})
        return json.dumps(case_subscription.subscriptions_as_json())
    elif action == 'delete':
        delete_cases([case_name])
        return json.dumps(case_subscription.subscriptions_as_json())
    elif action == 'edit':
        form = forms.EditCaseForm(request.form)
        if form.validate():
            if form.name.data:
                rename_case(case_name, form.name.data)
                if form.note.data:
                    case_database.case_db.edit_case_note(form.name.data, form.note.data)
            elif form.note.data:
                case_database.case_db.edit_case_note(case_name, form.note.data)
            return json.dumps(case_database.case_db.cases_as_json())
    else:
        return json.dumps({"status": "Invalid operation {0}".format(action)})


@app.route('/cases/<string:case_name>', methods=['GET'])
@auth_token_required
@roles_accepted(*userRoles['/cases'])
def display_case(case_name):
    case = case_database.case_db.session.query(case_database.Case) \
        .filter(case_database.Case.name == case_name).first()
    if case:
        return json.dumps({'case': case.as_json()})
    else:
        return json.dumps({'status': 'Case with given name does not exist'})


@app.route('/cases/event/<int:event_id>/edit', methods=['POST'])
@auth_token_required
@roles_accepted(*userRoles['/cases'])
def edit_event_note(event_id):
    form = forms.EditEventForm(request.form)
    if form.validate():
        if form.note.data:
            valid_event_id = case_database.case_db.session.query(case_database.Event) \
                .filter(case_database.Event.id == event_id).all()
            if valid_event_id:
                case_database.case_db.edit_event_note(event_id, form.note.data)
                return json.dumps(case_database.case_db.event_as_json(event_id))
            else:
                return json.dumps({"status": "invalid event"})
    else:
        return json.dumps({"status": "Invalid form"})


@app.route('/cases/subscriptions/available', methods=['GET'])
@auth_token_required
@roles_accepted(*userRoles['/cases'])
def display_possible_subscriptions():
    with open(os.path.join('.', 'data', 'events.json')) as f:
        return f.read()


@app.route('/cases/subscriptions/<string:case_name>/global/edit', methods=['POST'])
@auth_token_required
@roles_accepted(*userRoles['/cases'])
def edit_global_subscription(case_name):
    form = forms.EditGlobalSubscriptionForm(request.form)
    if form.validate():
        global_sub = case_subscription.GlobalSubscriptions(controller=form.controller.data,
                                                           workflow=form.workflow.data,
                                                           step=form.step.data,
                                                           next_step=form.next_step.data,
                                                           flag=form.flag.data,
                                                           filter=form.filter.data)
        success = case_subscription.edit_global_subscription(case_name, global_sub)
        if success:
            return json.dumps(case_subscription.subscriptions_as_json())
        else:
            return json.dumps({"status": "Error: Case name {0} was not found".format(case_name)})
    else:
        return json.dumps({"status": "Error: form invalid"})


@app.route('/cases/subscriptions/<string:case_name>/subscription/<string:action>', methods=['POST'])
@auth_token_required
@roles_accepted(*userRoles['/cases'])
def crud_subscription(case_name, action):
    if action == 'edit':
        form = forms.EditSubscriptionForm(request.form)
        if form.validate():
            success = case_subscription.edit_subscription(case_name, form.ancestry.data, form.events.data)
            if success:
                return json.dumps(case_subscription.subscriptions_as_json())
            else:
                return json.dumps({"status": "Error occurred while editing subscription"})
        else:
            return json.dumps({"status": "Error: Case name {0} was not found".format(case_name)})
    elif action == 'add':
        form = forms.AddSubscriptionForm(request.form)
        if form.validate():
            case_subscription.add_subscription(case_name, form.ancestry.data, form.events.data)
            return json.dumps(case_subscription.subscriptions_as_json())
    elif action == 'delete':
        form = forms.DeleteSubscriptionForm(request.form)
        if form.validate():
            case_subscription.remove_subscription_node(case_name, form.ancestry.data)
            return json.dumps(case_subscription.subscriptions_as_json())


@app.route('/cases/subscriptions/', methods=['GET'])
@auth_token_required
@roles_accepted(*userRoles['/cases'])
def display_subscriptions():
    return json.dumps(case_subscription.subscriptions_as_json())


@app.route("/configuration/<string:key>", methods=['POST'])
@auth_token_required
@roles_accepted(*userRoles["/configuration"])
def configValues(key):
    if current_user.is_authenticated and key:
        if hasattr(config, key):
            return json.dumps({str(key): str(getattr(config, key))})


# Returns System-Level Interface Pages
@app.route('/interface/<string:name>/display', methods=["POST"])
@auth_token_required
@roles_accepted(*userRoles["/interface"])
def systemPages(name):
    if current_user.is_authenticated and name:
        args = getattr(interface, name)()
        return render_template("pages/" + name + "/index.html", **args)
    else:
        return {"status": "Could Not Log In."}


# Controls execution triggers
@app.route('/execution/listener', methods=["POST"])
@auth_token_required
@roles_accepted(*userRoles["/execution/listener"])
def listener():
    form = forms.incomingDataForm(request.form)
    listener_output = Triggers.execute(form.data.data) if form.validate() else {}
    return json.dumps(listener_output)


@app.route('/execution/listener/triggers', methods=["GET"])
@auth_token_required
@roles_accepted(*userRoles["/execution/listener/triggers"])
def displayAllTriggers():
    result = str(Triggers.query.all())
    return result


@app.route('/execution/listener/triggers/<string:action>', methods=["POST"])
@auth_token_required
@roles_accepted(*userRoles["/execution/listener/triggers"])
def triggerManagement(action):
    if action == "add":
        form = forms.addNewTriggerForm(request.form)
        if form.validate():
            query = Triggers.query.filter_by(name=form.name.data).first()
            if query is None:
                database.db.session.add(
                    Triggers(name=form.name.data, condition=json.dumps(form.conditional.data), play=form.play.data))

                database.db.session.commit()
                return json.dumps({"status": "trigger successfully added"})
            else:
                return json.dumps({"status": "trigger with that name already exists"})
        return json.dumps({"status": "trigger could not be added"})


@app.route('/execution/listener/triggers/<string:name>/<string:action>', methods=["POST"])
@auth_token_required
@roles_accepted(*userRoles["/execution/listener/triggers"])
def triggerFunctions(action, name):
    if action == "edit":
        form = forms.editTriggerForm(request.form)
        trigger = Triggers.query.filter_by(name=name).first()
        if form.validate() and trigger is not None:
            # Ensures new name is unique
            if form.name.data:
                if len(Triggers.query.filter_by(name=form.name.data).all()) > 0:
                    return json.dumps({"status": "device could not be edited"})

            result = trigger.editTrigger(form)

            if result:
                db.session.commit()
                return json.dumps({"status": "device successfully edited"})

        return json.dumps({"status": "device could not be edited"})

    elif action == "remove":
        query = Triggers.query.filter_by(name=name).first()
        if query:
            Triggers.query.filter_by(name=name).delete()
            database.db.session.commit()
            return json.dumps({"status": "removed trigger"})
        elif query is None:
            json.dumps({"status": "trigger does not exist"})
        return json.dumps({"status": "could not remove trigger"})

    elif action == "display":
        query = Triggers.query.filter_by(name=name).first()
        if query:
            return str(query)
        return json.dumps({"status": "could not display trigger"})


# Controls roles
@app.route('/roles/<string:action>', methods=["POST"])
@auth_token_required
@roles_accepted(*userRoles["/roles"])
def roleAddActions(action):
    # Adds a new role
    if action == "add":
        form = forms.NewRoleForm(request.form)
        if form.validate():
            if not database.Role.query.filter_by(name=form.name.data).first():
                n = form.name.data

                if form.description.data is not None:
                    d = form.description.data
                    user_datastore.create_role(name=n, description=d, pages=default_urls)
                else:
                    user_datastore.create_role(name=n, pages=default_urls)

                database.add_to_userRoles(n, default_urls)

                db.session.commit()
                return json.dumps({"status": "role added " + n})
            else:
                return json.dumps({"status": "role exists"})
        else:
            return json.dumps({"status": "invalid input"})
    else:
        return json.dumps({"status": "invalid input"})


@app.route('/roles/<string:action>/<string:name>', methods=["POST"])
@auth_token_required
@roles_accepted(*userRoles["/roles"])
def roleActions(action, name):
    role = database.Role.query.filter_by(name=name).first()

    if role:

        if action == "edit":
            form = forms.EditRoleForm(request.form)
            if form.validate():
                if form.description.data:
                    role.setDescription(form.description.data)
                if form.pages.data:
                    database.add_to_userRoles(name, form.pages)
            return json.dumps(role.display())

        elif action == "display":
            return json.dumps(role.display())
        else:
            return json.dumps({"status": "invalid input"})

    return json.dumps({"status": "role does not exist"})


# Controls non-specific users and roles
@app.route('/users/<string:action>', methods=["POST"])
@auth_token_required
@roles_accepted(*userRoles["/users"])
def userNonSpecificActions(action):
    # Adds a new user
    if action == "add":
        form = forms.NewUserForm(request.form)
        if form.validate():
            if not database.User.query.filter_by(email=form.username.data).first():
                un = form.username.data
                pw = encrypt_password(form.password.data)

                # Creates User
                u = user_datastore.create_user(email=un, password=pw)

                if form.role.entries:
                    u.setRoles(form.role.entries)

                db.session.commit()
                return json.dumps({"status": "user added " + str(u.id)})
            else:
                return json.dumps({"status": "user exists"})
        else:
            return json.dumps({"status": "invalid input"})


# Controls non-specific users and roles
@app.route('/users', methods=["POST"])
@auth_token_required
@roles_accepted(*userRoles["/users"])
def displayAllUsers():
    result = str(User.query.all())
    return result


# Controls non-specific users and roles
@app.route('/users/<string:id_or_email>', methods=["POST"])
@auth_token_required
@roles_accepted(*userRoles["/users"])
def displayUser(id_or_email):
    user = user_datastore.get_user(id_or_email)
    if user:
        return json.dumps(user.display())
    else:
        return json.dumps({"status": "could not display user"})


# Controls users and roles
@app.route('/users/<string:id_or_email>/<string:action>', methods=["POST"])
@auth_token_required
@roles_accepted(*userRoles["/users"])
def userActions(action, id_or_email):
    user = user_datastore.get_user(id_or_email)
    if user:
        if action == "remove":
            if user != current_user:
                user_datastore.delete_user(user)
                db.session.commit()
                return json.dumps({"status": "user removed"})
            else:
                return json.dumps({"status": "user could not be removed"})

        elif action == "edit":
            form = forms.EditUserForm(request.form)
            if form.validate():
                if form.password:
                    verify_and_update_password(form.password.data, user)
                if form.role.entries:
                    user.setRoles(form.role.entries)

            return json.dumps(user.display())

        elif action == "display":
            if user is not None:
                return json.dumps(user.display())
            else:
                return json.dumps({"status": "could not display user"})


# Controls the non-specific app device configuration
@app.route('/configuration/<string:app>/devices', methods=["POST"])
@auth_token_required
@roles_accepted(*userRoles["/configuration"])
def listDevices(app):
    query = running_context.Device.query.all()
    output = []
    if query:
        for device in query:
            for app_elem in device.apps:
                if app_elem.app == app:
                    output.append(device.as_json())
    return json.dumps(output)


# Controls the non-specific app device configuration
@app.route('/configuration/<string:app>/devices/<string:action>', methods=["POST"])
@auth_token_required
@roles_accepted(*userRoles["/configuration"])
def configDevicesConfig(app, action):
    if action == "add":
        form = forms.AddNewDeviceForm(request.form)
        if form.validate():
            if len(running_context.Device.query.filter_by(name=form.name.data).all()) > 0:
                return json.dumps({"status": "device could not be added"})

            running_context.Device.add_device(name=form.name.data, username=form.username.data,
                                              password=form.pw.data, ip=form.ipaddr.data, port=form.port.data,
                                              app_server=app,
                                              extraFields=form.extraFields.data)

            return json.dumps({"status": "device successfully added"})
        return json.dumps({"status": "device could not be added"})
    if action == "all":
        query = running_context.App.query.filter_by(name=app).first()
        output = []
        if query:
            for device in query.devices:
                output.append(device.as_json())

            return json.dumps(output)
    return json.dumps({"status": "could not display all devices"})


# Controls the specific app device configuration
@app.route('/configuration/<string:app>/devices/<string:device>/<string:action>', methods=["POST"])
@auth_token_required
@roles_accepted(*userRoles["/configuration"])
def configDevicesConfigId(app, device, action):
    if action == "display":
        dev = running_context.Device.query.filter_by(name=device).first()
        if dev is not None:
            return json.dumps(dev.as_json())
        return json.dumps({"status": "could not display device"})

    elif action == "remove":
        dev = running_context.Device.query.filter_by(name=device).first()
        if dev is not None:
            dev.delete()
            db.session.commit()
            return json.dumps({"status": "removed device"})
        return json.dumps({"status": "could not remove device"})

    elif action == "edit":
        form = forms.EditDeviceForm(request.form)
        dev = running_context.Device.query.filter_by(name=device).first()
        if form.validate() and dev is not None:
            # Ensures new name is unique
            # if len(devClass.query.filter_by(name=str(device)).all()) > 0:
            #     return json.dumps({"status": "device could not be edited"})

            dev.editDevice(form)

            db.session.commit()
            return json.dumps({"status": "device successfully edited"})
        return json.dumps({"status": "device could not be edited"})


# Start Flask
def start(config_type=None):
    global db, env

    if config.https.lower() == "true":
        # Sets up HTTPS
        if config.TLS_version == "1.2":
            context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
        elif config.TLS_version == "1.1":
            context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_1)
        else:
            context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)

        # Provide user with informative error message
        displayIfFileNotFound(config.certificatePath)
        displayIfFileNotFound(config.privateKeyPath)

        context.load_cert_chain(config.certificatePath, config.privateKeyPath)
        app.run(debug=config.debug, ssl_context=context, host=config.host, port=int(config.port), threaded=True)
    else:
        app.run(debug=config.debug, host=config.host, port=int(config.port), threaded=True)


def displayIfFileNotFound(filepath):
    if not os.path.isfile(filepath):
        print("File not found: " + filepath)