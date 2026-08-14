from scipy.signal import firwin
import numpy as np
import math

import ostb._ostb as ostb


''' --- Parameters --- '''
speed_of_sound = 1540.0
frame_count = 16
sample_factor = 4
gain_analog_db = 40.0
time_slot_seconds = 1.0e-3

active_element_count = 16
focus_depth_m = 30.0e-3
include_last_aperture = True

z_min_m, z_max_m = 20.0e-3, 70.0e-3
scan_start_s = 2.0 * z_min_m / speed_of_sound
scan_range_s = 2.0 * z_max_m / speed_of_sound - scan_start_s

center_frequency_hz = 3.5e6
sampling_frequency_hz = 100.0e6 / float(sample_factor)

''' --- make bandpass filter coeffs --- '''
taps = firwin(
    numtaps=17,
    cutoff=[1.5e6, 5.5e6],
    window="hann",
    pass_zero=False,
    fs=sampling_frequency_hz,
)

''' --- Probe + waveform + trigger + root --- '''
probe = ostb.ProbeFactory("C3.5-128R60C")
probe.validate()

waveform = ostb.WaveformFactory.make_unipolar(center_frequency_hz)
waveform.set_awg(False)
waveform.set_negative_voltage(50.0)
waveform.validate()

trigger = (
    ostb.TriggerBuilder()
    .set_mode(ostb.TriggerMode.INTERNAL)
    .add_output_route(
        ostb.DigitalOutputPin.DIGITAL_OUTPUT_07,
        ostb.TriggerSignal.SIGNAL_CYCLE)
    .build())
trigger.validate()

root = (
    ostb.RootBuilder()
    .set_enable_fmc(True)
    .set_fmc_element_start(0)
    .set_fmc_element_stop(probe.element_count - 1)
    .set_fmc_element_step(1)
    .set_usb3_disable(True)
    .set_enable_awg(False)
    .set_matrix_callback(True)
    .set_ascan_bit_size(ostb.AscanBitSize.BITS_16)
    .build())

''' --- build sequence --- '''
seqBuilder = (ostb.SequenceBuilder()
               .set_probe(probe)
               .set_trigger(trigger)
               .set_number_of_frames(frame_count))

rxConfig = (ostb.RxConfigBuilder()
          .set_apodization([1.0] * probe.element_count)
          .set_gain([0.0] * probe.element_count)
          .set_focus(ostb.RxFocus.STANDARD)
          .set_delays([0.0] * probe.element_count)
          .build(probe.element_count))

line_count = (probe.element_count - active_element_count
                  + (1 if include_last_aperture else 0))

centers = []
for line_idx in range(line_count):
    start = line_idx
    apodization = [0.0] * probe.element_count
    apodization[start: start + active_element_count] = [1.0] * active_element_count

    # center element index of the active TX aperture
    center_idx = start + active_element_count // 2 - (
        1 if (active_element_count % 2 == 0) else 0
    )
    centers.append(center_idx)

    # convex steering angle and radius from probe geometry
    azimuth_rad = probe.az_angle[center_idx]
    radius_m = probe.radius

    # convex focal point calculation
    rho_m = radius_m + focus_depth_m
    x_focus_m = rho_m * math.sin(azimuth_rad)
    z_focus_m = -radius_m + rho_m * math.cos(azimuth_rad)

    ''' --- setup txConfig --- '''
    txConfig = (ostb.TxConfigBuilder()
          .set_apodization(apodization)
          .set_source([0.0, 0.0, 0.0])
          .set_focus_point([x_focus_m, 0.0, z_focus_m])
          .set_tx_with_all_elements(False)
          .set_waveform(waveform)
          .set_speed_of_sound(speed_of_sound)
          .compute_delays(probe)
          .build(probe))

    ''' --- setup scan cycle --- '''
    scan = (ostb.ScanBuilder()
            .set_tx(txConfig)
            .set_rx(rxConfig)
            .set_process_id(0)
            .set_scan_id(line_idx)
            .set_frame_id(0)
            .set_gain_digital(0.0)
            .set_gain_analog(gain_analog_db)
            .set_start(scan_start_s)
            .set_range(scan_range_s)
            .set_time_slot(time_slot_seconds)
            .set_sample_factor(sample_factor)
            .set_compression_type(ostb.CompressionType.DECIMATION)
            .set_rectification(ostb.Rectification.SIGNED)
            .set_beam_correction(0.0)
            .build(probe.element_count))

    ''' --- add scan to sequence --- '''
    seqBuilder.add_scan(scan)

''' --- build the sequence --- '''
seq = seqBuilder.build()

''' --- build imaging grid --- '''
multi_line_count = 12

azimuth_axis = np.linspace(probe.az_angle[centers[0]], probe.az_angle[centers[-1]], multi_line_count * line_count)
radial_axis = np.linspace(z_min_m + probe.radius, z_max_m + probe.radius, 512).astype(np.float32)

polar_image_grid = (
    ostb.ImageGridBuilder()
    .set_geometry(ostb.ImageGridGeometry.POLAR)
    .set_layout(ostb.ImageGridLayout.PLANAR_2D)
    .set_radial_coordinates(radial_axis)
    .set_azimuth_coordinates(azimuth_axis)
    .set_elevation_coordinates([0.0])
    .set_apex([0.0, 0.0, -probe.radius])
    .build()
)

polar_bounds = polar_image_grid.bounds
display_x_coordinates = np.linspace(
    float(polar_bounds.x_limits[0]),
    float(polar_bounds.x_limits[1]),
    512,
)
display_z_coordinates = np.linspace(
    float(polar_bounds.z_limits[0]),
    float(polar_bounds.z_limits[1]),
    512,
)

display_image_grid = (
    ostb.ImageGridBuilder()
    .set_geometry(ostb.ImageGridGeometry.RECTANGULAR)
    .set_layout(ostb.ImageGridLayout.PLANAR_2D)
    .set_x_coordinates(display_x_coordinates)
    .set_y_coordinates([0.0])
    .set_z_coordinates(display_z_coordinates)
    .build()
)
display_image_grid.validate()

''' --- build render window --- '''
render_window = (
    ostb.RenderWindowBuilder()
    .set_type(ostb.RenderWindowType.IMAGE)
    .set_title("C3.5-128R60C Focused B-Mode")
    .set_x_label("Lateral")
    .set_y_label("Depth")
    .add_image_layer(
        display_image_grid,
        process_id=0,
        colormap=ostb.RenderColormap.GRAY,
        opacity=1.0,
        label=str(""),
        visible=True,
    )
    .build())

''' --- post processing pipeline --- '''
tx_apod = ostb.TxApodizationConfig()
tx_apod.window = ostb.ApodizationType.FOCUSED
tx_apod.mla = multi_line_count
tx_apod.mla_overlap = 6
tx_apod.validate()

rx_apod = ostb.RxApodizationConfig()
rx_apod.f_number = [1.2, 1.2]
rx_apod.window = ostb.ApodizationType.RECTANGULAR
rx_apod.validate()

beamforming_setup = ostb.BeamformingSetup()
beamforming_setup.speed_of_sound = speed_of_sound
beamforming_setup.tx_apodization = tx_apod
beamforming_setup.rx_apodization = rx_apod
beamforming_setup.validate()

bp = ostb.BandPassFilterConfig()
bp.coeffs = [float(x) for x in taps]

das = ostb.DasBmodeConfig()
das.beamform.setup = beamforming_setup

tgc = ostb.AutoTgcConfig()
tgc.max_fit_point_count = 256
tgc.min_gain_near = 0.75
tgc.min_gain_far = 0.75
tgc.max_gain_near = 2.0
tgc.max_gain_far = 8.0

log = ostb.LogCompressionConfig()
log.gain_db = 50
log.dynamic_range_db = 60
log.epsilon = 1.0e-6

scanconverter = ostb.ScanConversionConfig()
scanconverter.output_sample_count = 512
scanconverter.output_line_count = 512
scanconverter.background_value = 0.0

clarity = ostb.ClarityConfig()
clarity.iteration_count = 15
clarity.lambda_ = 0.25
clarity.sigma_x = 6.0
clarity.sigma_y = 6.0
clarity.sigma_i = 15.0
clarity.reject = 3.0

pb = ostb.ProcessBuilder()
pb.add_band_pass_filter(bp)
pb.add_das_bmode_beamform(das)
pb.add_auto_tgc(tgc)
pb.add_log_compression(log)
pb.add_scan_conversion(scanconverter)
pb.add_clarity(clarity)
processes = pb.build()
processes.validate()


bmode_entry = ostb.ProcessPipelineEntry()
bmode_entry.image_grid = polar_image_grid
bmode_entry.processes = processes

processing = ostb.AcquisitionProcessingConfig()
processing.set_enable_beamforming(True)
processing.set_enable_display(True)
processing.set_image_grid(polar_image_grid)
processing.set_render_windows([render_window])
processing.set_process_pipeline(0, bmode_entry)
processing.validate()


configuration = ostb.AcquisitionConfiguration()
configuration.set_root(root)
configuration.set_sequence(seq)
configuration.set_processing(processing)
configuration.validate()

connection = ostb.SystemConnectionSettings()
connection.set_ip_address("192.168.1.11")
connection.set_port(4096)
connection.set_connect_timeout_ms(10000)
connection.set_hardware_type(ostb.HardwareType.OEM_PA_MAX)
connection.validate()


runtime = ostb.AcquisitionRuntime()
print("Connecting to Hardware...", end="")
runtime.connect(connection)
print(" OK")
print("Writing Hardware Configuration...", end="")
runtime.configure(configuration)
print(" OK")

''' --- Explicit Control Functions --- '''

def start_acquisition() -> bool:
    """Starts ultrasound hardware scan directly via OSTB runtime."""
    print("[curv_proper_code] Executing start_acquisition()")
    try:
        if hasattr(runtime, 'start'):
            runtime.start()
            return True
    except Exception as e:
        print(f"[curv_proper_code] Error starting acquisition: {e}")
    return False


def stop_acquisition() -> bool:
    """Stops/Freezes ultrasound hardware scan directly via OSTB runtime."""
    print("[curv_proper_code] Executing stop_acquisition()")
    try:
        if hasattr(runtime, 'stop'):
            runtime.stop()
            return True
    except Exception as e:
        print(f"[curv_proper_code] Error stopping acquisition: {e}")
    return False


def set_voltage(val: float) -> bool:
    """Sets negative voltage on waveform and updates runtime configuration."""
    print(f"[curv_proper_code] Executing set_voltage({val})")
    try:
        v = max(0.0, min(50.0, float(val)))
        waveform.set_negative_voltage(v)
        runtime.configure(configuration)
        return True
    except Exception as e:
        print(f"[curv_proper_code] Error setting voltage: {e}")
    return False


def set_gain(val: float) -> bool:
    """Sets analog gain and updates runtime configuration."""
    print(f"[curv_proper_code] Executing set_gain({val})")
    try:
        global gain_analog_db
        gain_analog_db = max(0.0, min(40.0, float(val)))
        runtime.configure(configuration)
        return True
    except Exception as e:
        print(f"[curv_proper_code] Error setting gain: {e}")
    return False


def set_display(enabled: bool) -> bool:
    """Toggles display rendering in acquisition processing configuration."""
    print(f"[curv_proper_code] Executing set_display({enabled})")
    try:
        processing.set_enable_display(bool(enabled))
        runtime.configure(configuration)
        return True
    except Exception as e:
        print(f"[curv_proper_code] Error setting display: {e}")
    return False


def get_current_status() -> dict:
    """
    Returns current control panel state based directly on curv_proper_code.py parameters.
    Used by Doctor control panel on page load to read initial values.
    """
    return {
        "status": "RUNNING" if getattr(runtime, 'is_running', False) else "STOPPED",
        "voltage": float(getattr(waveform, 'negative_voltage', 50.0)),
        "gain": float(gain_analog_db),
        "display": bool(getattr(processing, 'enable_display', True)),
        "tgc_enabled": False,
        "tgc_sliders": [50, 12, 3, 77, 90, 30]
    }


runtime.start_control_server()
runtime.run_controller()

print("Saving Acquired Data...", end="")
ostb.save_acquisition_archive_hdf5(
    configuration,
    "capture_focused_curvilinear.h5",
    include_rf_data=True,
    include_process_output_data=True,
)
print(" OK")

runtime.stop_control_server()
runtime.disconnect()
print("Done.")