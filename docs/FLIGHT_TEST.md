# Flight test checklist

One VATSIM session that exercises every path. Watch Developer Tools > States (filter `vatsim`) and the AppDaemon log. Pick a departure with ATIS online and a route with at least one airway. For the NAT step, file an oceanic route with a track token (`NATA` to `NATZ`) while `nat_track_status` is `active`.

Remember the attribute contract: a flag that is false, and a count that is zero, is **absent**, not `false` or `0`.

| # | Do this | Expect |
|---|---|---|
| 0 | Before connecting | `sensor.vatsim_feed_health` = `healthy`, `geometry_status: ready`, `navdata_status: active`. `binary_sensor.vatsim_pilot_connected` = `off`, `presence_state: absent`. `sensor.vatsim_destination` = `not_connected` |
| 1 | Connect at the gate with a flight plan filed | Within 2 polls (about 40 s): `binary_sensor.vatsim_pilot_connected` = `on`, `presence_state: present`. `device_tracker.vatsim_aircraft` has `latitude`, `longitude`, your `callsign`, `session_active: true`. `binary_sensor.vatsim_aircraft_has_position` = `on` |
| 2 | Still parked | `sensor.vatsim_next_sector` = `skipped_low_speed`. `sensor.vatsim_destination` = `departure`, `role: departure`, `airport` = your departure, `atis_code` set if an ATIS is online |
| 3 | Check the route | `sensor.vatsim_flight_route` = `ok`, `departure` and `arrival` correct, `resolved_waypoint_count` plausible, `unresolved_token_count` absent or equal to the number of SID/STAR names. The dashed line on the map follows the airway, with no doubled corner at an airway exit |
| 4 | ATIS letter changes while parked (wait for the hourly update, with the ATIS alert toggle on) | One announcement: "<airport> ATIS is now information <Letter>". No announcement when you only connect, take off, or change airports |
| 5 | Take off | After 2 polls above 40 kt with a 300 ft climb: `sensor.vatsim_destination` leaves `departure` for `out_of_range` (or `in_range` on a short hop), `role: arrival`. `sensor.vatsim_session_track` = `active`, `point_count` rising |
| 6 | Climb out on a steady heading | After 3 polls: `sensor.vatsim_next_sector` leaves `skipped_insufficient_history` for `matches` or `differs` |
| 7 | Enter airspace with a controller online, or have one come online | `binary_sensor.vatsim_atc_coverage` = `on`, `controller_callsign`, `frequency`, `mode`; `last_change_reason: became_controlled`. With the ATC online toggle on: one announcement about 20 s later. The map shades the sector green |
| 8 | Fly toward a different online sector | `sensor.vatsim_next_sector` = `differs`, `result_kind: different_sector`, `controller_callsign`, `eta_text`, `eta_distance_nm`. With the lookahead toggle on: one "Next likely controller" announcement. The map shades it amber |
| 9 | Make a turn of more than 20 degrees | `sensor.vatsim_next_sector` = `skipped_unstable_heading` during the turn, then recovers within 3 polls |
| 10 | Above 18,000 ft over a field with approach online | Coverage ignores the approach sector; `projection_note: altitude_gate` appears on `sensor.vatsim_next_sector` when it applies |
| 11 | Oceanic leg on a NAT track | The route line follows the track's coordinates; `nat_track_status: active`. With an expired or absent track the oceanic part is a straight line between the entry and exit fixes, never a stale track |
| 12 | Within 100 nm of the arrival | `sensor.vatsim_destination` = `in_range`, `distance_nm` counting down, arrival ATIS shown |
| 13 | Type another airport into the ATIS box | `sensor.vatsim_destination` = `selected`, `role: selected`. Clear it to return to automatic |
| 14 | Disconnect | After 3 polls (about 1 min): `binary_sensor.vatsim_pilot_connected` = `off`. Coverage `off` with `last_change_reason: cleared_on_absence`. `sensor.vatsim_session_track` = `empty`. `sensor.vatsim_flight_route` = `no_flight_plan`. `device_tracker.vatsim_aircraft` keeps the last `latitude` and `longitude`; `session_active` is absent |
| 15 | Restart Home Assistant (not AppDaemon) | Entities vanish, then return within one poll. The log shows `HA does not hold ... republishing` once at INFO. No ATC announcement is repeated |
| 16 | Restart the AppDaemon add-on | Log: `Loaded saved aircraft position`. The map still shows the last position |

Failure drills (optional, on the ground):

| Do this | Expect |
|---|---|
| Set `navdata_path` to `/config/vatsim/navdata` and restart AppDaemon | One ERROR in the log naming the resolved path and the four files. `navdata_status: configured_but_unloaded`, `last_navdata_refresh_error` set. After 5 min: a "VATSIM navdata degraded" push carrying the error |
| Stop the AppDaemon add-on | After about 3 min: a "VATSIM tracker not publishing" push and sidebar notice, whatever the alert mode |
| Set the alert mode to `Disabled` and repeat either drill | The health push still arrives |
