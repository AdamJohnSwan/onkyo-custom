# Onkyo 

Making this to deal with errors with my Integra stereo while I wait for pyeiscp to accept my PR.

The `onkyo` {% term integration %} allows you to control a [Onkyo](https://www.onkyo.com), [Integra](http://www.integrahometheater.com)
and some recent [Pioneer](https://www.pioneerelectronics.com) receivers from Home Assistant.
Please be aware that you need to enable "Network Standby" for this integration to work in your Hardware.

## Configuration

To add an Onkyo or Pioneer receiver to your installation, add the following to your {% term "`configuration.yaml`" %} file.
{% include integrations/restart_ha_after_config_inclusion.md %}

```yaml
# Example configuration.yaml entry
media_player:
  - platform: onkyo
    host: 192.168.1.2
    name: receiver
    sources:
      pc: "HTPC"
```