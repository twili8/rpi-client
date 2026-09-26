# rpi-client

**PS4 remote package installer toolkit in Python**. This can be used to install **fake packages** on your PS4. \
To use, install [this](https://github.com/flatz/ps4_remote_pkg_installer) pkg on your console. Install it via USB through HEN/Goldhen. Chicken and egg problem i guess. \

# How to use?
This will display a terminal interface. Input your desired operation and press enter: `python3 client.py`

### Tested on 13.52.

## Troubleshooting

If you encounter the errors listed below, you must resolve them manually on your PS4. These actions cannot be performed directly from this tool (though maybe one day I will add support for this!). The reason is that it would require modifying the server code and I don't really have the time to do all of that lol.

| Error | Code | Hex | Solution |
| :--- | :--- | :--- | :--- |
| `SCE_BGFT_ERROR_TASK_DUPLICATED` | CE-32928-4 | `0x80990015` | Delete the task in the PS4's task manager. |
| `SCE_APP_INSTALLER_ERROR_NOSPACE` | CE-33171-5 | `0x80a30002` | Not enough space on your PS4. Delete some data. |
| `SCE_APP_INSTALLER_ERROR_PKG_INVALID_DRM_TYPE` | CE-33175-9 | `0x80a30006` | Fakepkg DLC on real pkg ID. Install the fakepkg app instead. |
| `SCE_APP_INSTALLER_ERROR_SYSTEM_VERSION` | CE-34627-2 | `0x80a3000d` | Wrong OS version. Try a backport or remarry the package. |

## Why build this?
why would i install [Java 8](https://github.com/BenjaminFaal/ps4-remote-pkg-installer) to communicate with an API instead of python? \
The client only uses the python stdlib to avoid users having to setup complex environments and as a programming exercisce for myself. \
Note: The serve option binds to 0.0.0.0 by default which can be a privacy problem if you run this for long periods. Change it to your preferred interface.
