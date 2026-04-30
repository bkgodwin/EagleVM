from flask_wtf import FlaskForm
from wtforms import BooleanField, FileField, IntegerField, PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired, Email, EqualTo, Length, NumberRange, Optional


class LoginForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email()])
    password = PasswordField("Password", validators=[DataRequired()])
    submit = SubmitField("Log in")


class ChangePasswordForm(FlaskForm):
    current_password = PasswordField("Current password", validators=[DataRequired()])
    password = PasswordField("New password", validators=[DataRequired(), Length(min=8)])
    confirm = PasswordField("Confirm password", validators=[DataRequired(), EqualTo("password")])
    submit = SubmitField("Change password")


class CSVImportForm(FlaskForm):
    csv_file = FileField("CSV file", validators=[DataRequired()])
    submit = SubmitField("Import users")


class SettingsForm(FlaskForm):
    default_cpu = IntegerField("Default vCPU", validators=[DataRequired(), NumberRange(min=1, max=64)])
    default_ram = IntegerField("Default RAM (MB)", validators=[DataRequired(), NumberRange(min=1024, max=262144)])
    proxmox_host = StringField("Proxmox host", validators=[Optional(), Length(max=255)])
    proxmox_node = StringField("Proxmox node", validators=[Optional(), Length(max=128)])
    template_vm_id = IntegerField("Template VM ID", validators=[DataRequired(), NumberRange(min=1)])
    submit = SubmitField("Save settings")


class ResourceOverrideForm(FlaskForm):
    cpu = IntegerField("vCPU", validators=[DataRequired(), NumberRange(min=1, max=64)])
    ram = IntegerField("RAM (MB)", validators=[DataRequired(), NumberRange(min=1024, max=262144)])
    submit = SubmitField("Update")


class UserCreateForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email()])
    password = PasswordField("Password", validators=[DataRequired(), Length(min=8)])
    is_admin = BooleanField("Admin")
    submit = SubmitField("Create user")
