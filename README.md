![logo](logo.png)

Connect your Uconnect-enabled vehicle to Home Assistant. This integration has been made from the reverse-engineered Stellantis vehicle-branded apps and websites.

> This is an independently maintained fork of [hass-uconnect/hass-uconnect](https://github.com/hass-uconnect/hass-uconnect). It keeps the same integration domain and entity IDs, so it can replace the original without losing configuration or history, see [Switching from the original](#switching-from-the-original). Its focus is on not waking or unlocking the vehicle by accident, see [Safety](#safety-).

## Car Brands

US, Canada, EU & Asia regions are supported. Try a different region if the originally selected region does not authenticate.

- Abarth: Works ✅
- Alfa Romeo: Works ✅
- Chrysler: Works ✅
- Dodge: Works ✅
- Fiat: Works ✅
- Jeep: Works ✅
- Lancia: Works ✅
- Maserati: Unknown ❔
- Ram: Works ✅

## Tested Vehicles

- See tested and working vehicles at the following discussion post: https://github.com/hass-uconnect/hass-uconnect/discussions/5
- If your vehicle works, feel free to upload the year, make and model to the discussion post.

## Prerequisites 📃

- Home Assistant
- [HACS](https://www.hacs.xyz) (Home Assistant Community Store) 
- A vehicle using Uconnect cellular services (see [SiriusXM Guardian](#siriusxm-guardian-vehicles) below if your vehicle uses SXM Guardian)
- Check the links below:
  - Alfa Romeo: https://connect.alfaromeo.com
  - Chrysler: https://connect.chrysler.com
  - Dodge: https://connect.dodge.com
  - Fiat: https://connect.fiat.com
  - Jeep: https://connect.jeep.com
  - Maserati: https://connect.maserati.com
  - Ram: https://connect.ramtrucks.com

## Features ✔️

- Imports statistics like battery level 🔋, tire pressure ‍💨, odometer ⏲, fuel type, vehicle health report, maintenance history, stolen vehicle status etc. into Home Assistant
- **Vehicle Image**: Displays your vehicle's image as an entity. The image is downloaded once and cached locally for fast access
- **Extrapolated Battery**: For EVs, provides a real-time battery estimate between API updates by tracking charging rate and idle drain, the drain being learned only from spells where the odometer confirms the car did not move. Automatically rejects stale data that would show impossible values (e.g., battery dropping while charging), and does not learn from a reading it rejected, and correctly handles state transitions (e.g., idle to charging, charging to driving). Polling alone returns a cached SOC that can go unchanged for hours, so while charging the vehicle can be asked to push fresh data (a deep refresh) at the configured interval, within the limits described under [Safety](#safety-). The charging rate follows the vehicle's time-to-full until it can be measured from real SOC changes, and is preserved across sessions so extrapolation begins immediately on the next one
- **Charge schedules** (EMEA vehicles such as the Fiat 500e): each of the vehicle's schedule slots gets a switch to turn it on or off and time entities for its start and end; the `set_charge_schedule_slot` action also sets the days, charge to full and cabin priority. The current schedules are read right before each change, so changes made in the app are kept, and nothing is sent when a slot already has the requested values
- **Trip A/B, battery health and upload times**: the trip computers, the traction battery state of health where the vehicle reports it, and when the vehicle last uploaded its data
- **Diagnostics download** (with VIN, account, PIN and position removed), including when and why the vehicle was woken
- **Charging Rate**: Shows the current charging speed in %/hour. Picks the time-to-full matching the charger the vehicle reports, including a domestic socket, and reports nothing rather than another charger's figure when the connected one publishes none. Computed over a 60-minute sliding window of SOC readings for stable output even with integer SOC values and irregular polling. Falls back to a time-to-full estimate until the first SOC gain is seen, since a window with no new reading means the API has not updated rather than that charging has stopped, and returns to that estimate once the last measurement is older than the window itself
- **Reset Battery Learning**: Button to reset the learned charging correction factor and idle drain rate back to defaults, and to drop the charging rate carried over from the last session. Useful when changing chargers or if learned values have drifted, the next session then starts from the vehicle's own time-to-full. The correction factor only applies while the rate comes from the vehicle's time-to-full, a rate measured from actual SOC changes is used as is, including one carried over from an earlier session
- Multiple Brands: Abarth, Alfa Romeo, Chrysler, Dodge, Fiat, Jeep, Lancia, Maserati & Ram
- Multiple Regions: America, Canada, Europe & Asia
- Supports multiple cars on the same account 🚙🚗🚕
- Location tracking 🌍
- Live vehicle status such as windows/doors, and ignition status for supported vehicles
- Home Assistant zones (home 🏠, work 🏦 etc.) support
- Uses the same data source as the official app 📱
- Remote commands (unlock doors 🚪, switch HVAC 🧊 on , ...). **Use a service (action) to trigger commands**. Some commands may not work with all vehicles. With more than one vehicle known to Home Assistant, an action needs `device_id`; unlocking needs the Uconnect PIN as `code` unless that is turned off in the options
- Available commands are:
  - `Refresh Location`: Updates the vehicle location
  - `Deep Refresh`: Refreshes EV battery level
  - `Deep Refresh (V2)`: Refreshes EV battery level using V2 API
  - `Lights/Horn`: Trigger vehicle horn and lights
  - `Lights`: Trigger vehicle lights
  - `Preconditioning On/Off`: Toggle vehicle preconditioning
  - `Trunk Lock/Unlock` / `Liftgate Lock/Unlock`: Lock/Unlock trunk/liftgate (unlock takes `code`)
  - `Doors Lock/Unlock`: Lock/Unlock vehicle doors (unlock takes `code`)
  - `Engine On/Off`: Remotely starts/stops the vehicle engine
  - `Cabin Ventilation`: Activates cabin ventilation
  - `Set Charge Schedule`: Sets the EV charge schedule (accepts a JSON object, North American vehicles)
  - `Set Charge Schedule Slot`: Changes one charge schedule slot of an EMEA vehicle, the others stay as they are
  - `Charge Now`: Initiates EV charging
  - `Charge Now (V4)`: Initiates EV charging using V4 API
  - `HVAC On/Off`: Toggles the HVAC
  - `Comfort On/Off`: Another alternative to the above HVAC commands (depends on make/model)
  - `Update`: Asks the integration to update the data from the API immediately

## SiriusXM Guardian Vehicles

Some US-market vehicles use SiriusXM Guardian as their connected services provider
instead of the standard Uconnect cellular service. These vehicles were previously
reported as unsupported.

Analysis of the official Stellantis mobile apps (Ram, Chrysler, Wagoneer NAFTA) shows
that there are **no separate API endpoints** for SXM Guardian vehicles. All vehicles
use the same cloud API regardless of SDP type. The apps contain a legacy Mopar login
fallback that triggers a server-side account migration to Gigya when needed.

This suggests SXM Guardian vehicles should work once the account is migrated. However,
this has not been confirmed with a real SXM Guardian account yet.

If your SiriusXM Guardian vehicle does not appear in Home Assistant:

1. Install the official app for your brand (Jeep, Ram, Chrysler, Dodge, etc.)
2. Log in with your Mopar/SXM Guardian credentials
3. If the app prompts you to link or migrate your account, complete the process
4. Verify your vehicle is visible and functional in the official app
5. Use the same credentials with this integration

If your vehicle still does not appear after completing these steps, please open an
issue with your vehicle year, make, model, and whether it shows in the official app.

## What will NEVER work? ❌

- Things the Uconnect API does not support such as real time tracking or adjusting the audio volume.
- Some commands are vehicle specific and do not work across all makes and models.
- Some vehicles do not support live status for locks/windows/ignition. 

## Safety 🛡️

Every remote command wakes the vehicle's telematics unit, which draws on its 12 V battery. Reading data does not: polls only fetch what the cloud holds.

**Automatic wake-ups.** The only thing that wakes the vehicle on its own is the deep refresh while charging: one when a charge begins, three minutes after it is noticed so the vehicle has settled on its time-to-full, then one per interval of the option *Deep Refresh While Charging* (with 0 only the first). A deep refresh you requested that made the charge show up counts as the first one. A vehicle that is not charging is never woken automatically. Each refresh is only sent when all of this holds:

- the last poll worked and reports the vehicle as charging and not unplugged, with a known state of charge below 100 %
- the vehicle uploaded its charge data less than an hour ago for the first refresh of a charge, and less than 12 hours ago for the later ones. A charge reported long ago may have ended without the end reaching the cloud
- at least 15 minutes passed since Home Assistant started and since any command was sent to the vehicle, and the interval passed since the last refresh of this charge
- fewer than 24 automatic refreshes in the last 24 hours
- not 3 refreshes in a row without new charge data, and not 3 failures in a row; otherwise there are none until the next charge. A rejected PIN also ends them, until it is changed

Charging that pauses for less than 30 minutes, like a wallbox following solar surplus, counts as the same charge. These limits are kept per vehicle and written to disk before each wake-up, so they hold across restarts, crashes, reloads and several entries for the same car. The extrapolated battery sensor shows `last_deep_refresh`, `deep_refreshes_this_session` and `deep_refreshes_last_24h`. To have no automatic wake-ups at all, disable the *Extrapolated Battery* sensor, which runs them.

**Commands on request** are never delayed, except to catch automations stuck in a loop: a deep refresh or location refresh within 10 minutes of the last refresh (at most 48 a day), and more than 30 commands within an hour or 100 within a day. Locking and switching off always go out, unless they only repeat themselves. The same command again right after the vehicle confirmed it is skipped without an error. A switch is not sent "on" again while the vehicle reports it on, nor a charging level the vehicle already has.

**The PIN.** A PIN the backend rejected is not sent again until you change it in the options. Further wrong attempts can lock it, which also blocks the official app.

**Unlocking** needs the Uconnect PIN as the code by default, for the lock entities and the unlock actions alike, so an automation unlocking all locks, a scene, a voice assistant or an accidental tap cannot open the car. Five wrong codes pause unlocking for 5 minutes. Locking needs no code. This can be turned off in the options. In YAML, put the PIN in quotes (`code: "0123"`), a leading zero would otherwise change the number. Do not store the PIN as the lock's default code in the entity settings, that would skip the check.

**Bulk actions.** The trunk and liftgate are locks, not switches, so "turn off all switches" cannot open them. The remaining command switches (engine, preconditioning, climate, charging), the charge schedule entities and the charging level are configuration entities: they are not reached by turning off a whole area, floor or device, not bridged to HomeKit by default and not exposed to voice assistants. Switches exposed by earlier versions are taken out of Assist, Alexa and Google once; expose them again if you want them by voice. Actions with `entity_id: all`, or with a template listing every switch or button, do reach them though, Home Assistant makes no exception there. Leave the vehicle out of such actions.

**Privacy.** Error messages and debug logs leave out the password, tokens, the account ID, the VIN and the position. The diagnostics download leaves them out too.

## Switching from the original

Configuration, entities and history are kept: the domain, the entity IDs and the state classes are the same.

1. Make a backup (Settings → System → Backups).
2. In HACS, open the original Uconnect integration and choose **Remove**. This removes the files only. Do **not** delete the integration under Settings → Devices & services, and do **not** restart until step 4. Started without the files, Home Assistant offers to remove the configuration; if that happens, choose *Ignore* and carry on.
3. Add `https://github.com/815rx1/hass-uconnect` as a custom repository (type *Integration*), open it and choose **Download**. Under *Need a different version?* pick the newest `v0.6.0b…`. To be offered later betas, enable the *Pre-release* switch of this repository on the HACS device.
4. Restart Home Assistant.
5. Check the options, and add `code: "<your PIN>"` to automations that unlock the car.

What changes: unlocking asks for the PIN, the command switches move out of the generated dashboards and voice assistants (see [Safety](#safety-)), the trunk switch becomes a lock (the old switch shows as unavailable and can be deleted), and the deep refresh while charging comes every 30 minutes by default, where 0.5.4 refreshed once per charge; set 0 to keep that.

**Going back** works the same way: back up, **Remove** this repository in HACS, download the original (*Need a different version?* for a particular one, such as v0.5.4), then restart. Entities only this fork has show as unavailable and keep their history, and the trunk switch returns. There, unlocking no longer asks for a code, and the `set_charge_schedule_slot` action is missing. Saving the original's options drops this fork's own options, so check them when you come back. The wake-up history in `.storage/uconnect.wake_history` stays behind; it is harmless and used again on your return.

## How to install 🛠️

- Make sure you have [HACS](https://hacs.xyz/docs/use/#getting-started-with-hacs) already installed
- Add the [repository URL](https://github.com/815rx1/hass-uconnect) to your HACS custom repositories as type `integration`
- Install the integration and restart Home Assistant
- Go to your integrations configuration page once started and add the Uconnect integration
- Fill e-mail, password and optionally PIN if you want to issue commands
- Requires Home Assistant 2024.12 or newer

After the integration is initialized - you might want to go into its options and enable the creation of command entities:

![image](https://github.com/user-attachments/assets/587c9ec0-bbd0-4918-b84b-4235316a58cf)

The options are:

- **Scan Interval (min)**: how often the integration polls the API, 5 minutes by default
- **Pin**: needed to issue remote commands, set it here if it was left empty during setup. A change takes effect straight away, and clearing the field falls back to the PIN given during setup. A PIN entered when reauthenticating or reconfiguring replaces this one too
- **Deep Refresh While Charging (min, 0 = only at the start of a charge, 15 minimum)**: how often to ask an EV to push fresh data while it is charging, 30 minutes by default. Polling alone returns a cached state of charge, so without this the extrapolated battery has nothing to correct itself against. A deep refresh is always sent when a charge begins; with 0 that stays the only one. A change takes effect on the charge already under way. Requires a PIN and a vehicle that supports deep refresh. See [Safety](#safety-) for the limits that always apply, and for turning automatic refreshes off entirely
- **Add command entities (Button/Lock/Switch)**: create entities for the remote commands instead of using actions only
- **Require the Uconnect PIN to unlock**: on by default, unlocking then asks for the PIN as its code

## Built-in Vehicle Card

The integration includes a custom Lovelace card (`custom:uconnect-vehicle-card`) that is automatically registered on setup. It shows:

- **Header**: Vehicle name and VIN (or custom license plate)
- **Indicators**: Doors, windows, trunk or liftgate, charging status, ignition
- **SOC/Fuel bar**: Battery percentage with charging animation, range value
- **Vehicle image**: From the image entity (if available)
- **Mini map**: Live location from the device tracker
- **Quick info**: Location, range, charging state, tire pressure, 12V battery, odometer

To add the card: Edit a dashboard, add a card, search for "Uconnect Vehicle", and select your vehicle.

![card example](custom_components/uconnect/frontend/card-example.png)

Card configuration options (all toggleable):
- `device_id` (required): Select your vehicle
- `license_plate`: Display a license plate instead of VIN
- `show_indicators`, `show_range`, `show_image`, `show_map`, `show_buttons`: Toggle sections

## Useful Resources

Cards:
  - [Ultra Card](https://github.com/WJDDesigns/Ultra-Card)
  - [Vehicle Status Card](https://github.com/ngocjohn/vehicle-status-card)
