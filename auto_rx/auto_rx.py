#!/usr/bin/env python
#
# Radiosonde Auto RX Tools
#
# 2017-04 Mark Jessop <vk5qi@rfhead.net>
#
# Use the build.sh script in this directory to build the required binaries and move them to this directory.
#
# The following other packages are needed:
# rtl-sdr (for the rtl_power and rtl_fm utilities)
# sox
#
# The following Python packages are needed:
# - numpy
# - crcmod
#
# Instructions:
# Modify config parameters below as required. Take note of the APRS_USER and APRS_PASS values.
# Run with: python auto_rx.py
# A log file will be written to log/<timestamp>.log
#
#
# TODO:
# [ ] Fix user gain setting issues. (gain='automatic' = no decode?!)
# [ ] Use FSK demod from codec2-dev ? 
# [ ] Storage of flight information in some kind of database.
# [ ] Local frequency blacklist, to speed up scan times.

import numpy as np
import sys
import argparse
import logging
import datetime
import time
import os
import signal
import Queue
import subprocess
import traceback
from aprs_utils import *
from habitat_utils import *
from ozi_utils import *
from threading import Thread
from StringIO import StringIO
from findpeaks import *
from config_reader import *
from gps_grabber import *
from async_file_reader import AsynchronousFileReader


# Internet Push Globals
APRS_OUTPUT_ENABLED = False
HABITAT_OUTPUT_ENABLED = False

INTERNET_PUSH_RUNNING = True
internet_push_queue = Queue.Queue()

# Second Queue for OziPlotter outputs, since we want this to run at a faster rate.
OZI_PUSH_RUNNING = True
ozi_push_queue = Queue.Queue()


# Flight Statistics data
# stores copies of the telemetry dictionary returned by process_rs_line.
flight_stats = {
    'first': None,
    'apogee': None,
    'last': None
}


def run_rtl_power(start, stop, step, filename="log_power.csv",  dwell = 20, ppm = 0, gain = 'automatic', bias = False):
    """ Run rtl_power, with a timeout"""
    # rtl_power -f 400400000:403500000:800 -i20 -1 log_power.csv

    # Add a -T option if bias is enabled
    bias_option = "-T " if bias else ""

    # Added -k 30 option, to SIGKILL rtl_power 5 seconds after the regular timeout expires. 
    rtl_power_cmd = "timeout -k 30 %d rtl_power %s-f %d:%d:%d -i %d -1 -c 20%% -p %d %s" % (dwell+10, bias_option, start, stop, step, dwell, int(ppm), filename)
    logging.info("Running frequency scan.")
    ret_code = os.system(rtl_power_cmd)
    if ret_code == 1:
        logging.critical("rtl_power call failed!")
        return False
    else:
        return True

def read_rtl_power(filename):
    """ Read in frequency samples from a single-shot log file produced by rtl_power """

    # Output buffers.
    freq = np.array([])
    power = np.array([])

    freq_step = 0


    # Open file.
    f = open(filename,'r')

    # rtl_power log files are csv's, with the first 6 fields in each line describing the time and frequency scan parameters
    # for the remaining fields, which contain the power samples. 

    for line in f:
        # Split line into fields.
        fields = line.split(',')

        if len(fields) < 6:
            logging.error("Invalid number of samples in input file - corrupt?")
            raise Exception("Invalid number of samples in input file - corrupt?")

        start_date = fields[0]
        start_time = fields[1]
        start_freq = float(fields[2])
        stop_freq = float(fields[3])
        freq_step = float(fields[4])
        n_samples = int(fields[5])

        #freq_range = np.arange(start_freq,stop_freq,freq_step)
        samples = np.loadtxt(StringIO(",".join(fields[6:])),delimiter=',')
        freq_range = np.linspace(start_freq,stop_freq,len(samples))

        # Add frequency range and samples to output buffers.
        freq = np.append(freq, freq_range)
        power = np.append(power, samples)

    f.close()

    # Sanitize power values, to remove the nan's that rtl_power puts in there occasionally.
    power = np.nan_to_num(power)

    return (freq, power, freq_step)


def quantize_freq(freq_list, quantize=5000):
    """ Quantise a list of frequencies to steps of <quantize> Hz """
    return np.round(freq_list/quantize)*quantize

def detect_sonde(frequency, ppm=0, gain='automatic', bias=False):
    """ Receive some FM and attempt to detect the presence of a radiosonde. """

    # Add a -T option if bias is enabled
    bias_option = "-T " if bias else ""

    rx_test_command = "timeout 10s rtl_fm %s-p %d -M fm -s 15k -f %d 2>/dev/null |" % (bias_option, int(ppm), frequency) 
    rx_test_command += "sox -t raw -r 15k -e s -b 16 -c 1 - -r 48000 -t wav - highpass 20 2>/dev/null |"
    rx_test_command += "./rs_detect -z -t 8 2>/dev/null"

    logging.info("Attempting sonde detection on %.3f MHz" % (frequency/1e6))

    ret_code = os.system(rx_test_command)

    ret_code = ret_code >> 8

    if ret_code == 3:
        logging.info("Detected a RS41!")
        return "RS41"
    elif ret_code == 4:
        logging.info("Detected a RS92!")
        return "RS92"
    else:
        return None

def reset_rtlsdr():
    """ Attempt to perform a USB Reset on all attached RTLSDRs. This uses the usb_reset binary from ../scan"""
    lsusb_output = subprocess.check_output(['lsusb'])
    try:
        devices = lsusb_output.split('\n')
        for device in devices:
            if 'RTL2838' in device:
                # Found an rtlsdr! Attempt to extract bus and device number.
                # Expecting something like: 'Bus 001 Device 005: ID 0bda:2838 Realtek Semiconductor Corp. RTL2838 DVB-T'
                device_fields = device.split(' ')
                # Attempt to cast fields to integers, to give some surety that we have the correct data.
                device_bus = int(device_fields[1])
                device_number = int(device_fields[3][:-1])
                # Construct device address
                reset_argument = '/dev/bus/usb/%03d/%03d' % (device_bus, device_number)
                # Attempt to reset the device.
                logging.info("Resetting device: %s" % reset_argument)
                ret_code = subprocess.call(['./reset_usb', reset_argument])
                logging.debug("Got return code: %s" % ret_code)
            else:
                continue
    except:
        logging.error("Errors occured while attempting to reset USB device.")


def sonde_search(config, attempts = 5):
    """ Perform a frequency scan across the defined range, and test each frequency for a radiosonde's presence. """
    search_attempts = attempts

    sonde_freq = None
    sonde_type = None

    while search_attempts > 0:

        # Scan Band
        run_rtl_power(config['min_freq']*1e6, config['max_freq']*1e6, config['search_step'], ppm=config['rtlsdr_ppm'], gain=config['rtlsdr_gain'], bias=config['rtlsdr_bias'])

        # Read in result
        try:
            (freq, power, step) = read_rtl_power('log_power.csv')
            # Sanity check results.
            if step == 0 or len(freq)==0 or len(power)==0:
                raise Exception("Invalid file.")

        except Exception as e:
            traceback.print_exc()
            logging.error("Failed to read log_power.csv. Resetting RTLSDRs and attempting to run rtl_power again.")
            # no log_power.csv usually means that rtl_power has locked up and had to be SIGKILL'd. 
            # This occurs when it can't get samples from the RTLSDR, because it's locked up for some reason.
            # Issuing a USB Reset to the rtlsdr can sometimes solve this. 
            reset_rtlsdr()
            search_attempts -= 1
            time.sleep(10)
            continue


        # Rough approximation of the noise floor of the received power spectrum.
        power_nf = np.mean(power)

        # Detect peaks.
        peak_indices = detect_peaks(power, mph=(power_nf+config['min_snr']), mpd=(config['min_distance']/step), show = False)

        if len(peak_indices) == 0:
            logging.info("No peaks found on this pass.")
            search_attempts -= 1
            time.sleep(10)
            continue

        # Sort peaks by power.
        peak_powers = power[peak_indices]
        peak_freqs = freq[peak_indices]
        peak_frequencies = peak_freqs[np.argsort(peak_powers)][::-1]

        # Quantize to nearest x kHz
        peak_frequencies = quantize_freq(peak_frequencies, config['quantization'])
        logging.info("Peaks found at (MHz): %s" % str(peak_frequencies/1e6))

        # Run rs_detect on each peak frequency, to determine if there is a sonde there.
        for freq in peak_frequencies:
            detected = detect_sonde(freq, ppm=config['rtlsdr_ppm'], gain=config['rtlsdr_gain'], bias=config['rtlsdr_bias'])
            if detected != None:
                sonde_freq = freq
                sonde_type = detected
                break

        if sonde_type != None:
            # Found a sonde! Break out of the while loop and attempt to decode it.
            return (sonde_freq, sonde_type)
        else:
            # No sondes found :-( Wait and try again.
            search_attempts -= 1
            logging.warning("Search attempt failed, %d attempts remaining. Waiting %d seconds." % (search_attempts, config['search_delay']))
            time.sleep(config['search_delay'])

    # If we get here, we have exhausted our search attempts.
    logging.error("No sondes detected.")
    return (None, None)

def process_rs_line(line):
    """ Process a line of output from the rs92gps decoder, converting it to a dict """
    # Sample output:
    #   0      1        2        3            4         5         6      7   8     9  10
    # 106,M3553150,2017-04-30,05:44:40.460,-34.72471,138.69178,-263.83, 0.1,265.0,0.3,OK
    try:

        if line[0] != "{":
            return None

        rs_frame = json.loads(line)
        rs_frame['crc'] = True # the rs92ecc only reports frames that match crc so we can lie here
        rs_frame['temp'] = 0.0 #we don't have this yet
        rs_frame['humidity'] = 0.0
        rs_frame['datetime_str'] = rs_frame['datetime'].replace("Z","") #python datetime sucks
        rs_frame['short_time'] = rs_frame['datetime'].split(".")[0].split("T")[1]

        logging.info("TELEMETRY: %s,%d,%s,%.5f,%.5f,%.1f,%s" % (rs_frame['id'], rs_frame['frame'],rs_frame['datetime'], rs_frame['lat'], rs_frame['lon'], rs_frame['alt'], rs_frame['crc']))

        return rs_frame

    except:
        logging.error("Could not parse string: %s" % line)
        traceback.print_exc()
        return None

def update_flight_stats(data):
    """ Maintain a record of flight statistics. """
    global flight_stats

    # Save the current frame into the 'last' frame storage
    flight_stats['last'] = data

    # Is this our first telemetry frame?
    # If so, populate all fields in the flight stats dict with the current telemetry frame.
    if flight_stats['first'] == None:
        flight_stats['first'] = data
        flight_stats['apogee'] = data

    # Is the current altitude higher than the current peak altitude?
    if data['alt'] > flight_stats['apogee']['alt']:
        flight_stats['apogee'] = data



def calculate_flight_statistics():
    """ Produce a flight summary, for inclusion in the log file. """
    global flight_stats

    # Grab peak altitude.
    peak_altitude = flight_stats['apogee']['alt']

    # Grab last known descent rate
    descent_rate = flight_stats['last']['vel_v']

    # Calculate average ascent rate, based on data we have.
    # Wrap this in a try, in case we have time string parsing issues.
    try:
        if flight_stats['first'] == flight_stats['apogee']:
            # We have only caught a flight during descent. Don't calculate ascent rate.
            ascent_rate = -1.0
        else:
            ascent_height = flight_stats['apogee']['alt'] - flight_stats['first']['alt']
            start_time = datetime.datetime.strptime(flight_stats['first']['datetime_str'],"%Y-%m-%dT%H:%M:%S.%f")
            apogee_time = datetime.datetime.strptime(flight_stats['apogee']['datetime_str'],"%Y-%m-%dT%H:%M:%S.%f")
            ascent_time = (apogee_time - start_time).seconds
            ascent_rate = ascent_height/float(ascent_time)
    except:
        ascent_rate = -1.0

    stats_str = "Acquired %s at %s on %s, at %d m altitude.\n" % (flight_stats['first']['type'], flight_stats['first']['datetime_str'], flight_stats['first']['freq'], int(flight_stats['first']['alt']))
    stats_str += "Ascent Rate: %.1f m/s, Peak Altitude: %d, Descent Rate: %.1f m/s\n" % (ascent_rate, int(peak_altitude), descent_rate)
    stats_str += "Last Position: %.5f, %.5f, %d m alt, at %s\n" % (flight_stats['last']['lat'], flight_stats['last']['lon'], int(flight_stats['last']['alt']), flight_stats['last']['datetime_str'])
    stats_str += "Flight Path: https://aprs.fi/#!call=%s&timerange=10800&tail=10800\n" % flight_stats['last']['id']

    return stats_str

def decode_rs92(frequency, ppm=0, gain='automatic', bias=False, rx_queue=None, almanac=None, ephemeris=None, timeout=120):
    """ Decode a RS92 sonde """
    global latest_sonde_data, internet_push_queue, ozi_push_queue

    # Before we get started, do we need to download GPS data?
    if ephemeris == None:
        # If no ephemeris data defined, attempt to download it.
        # get_ephemeris will either return the saved file name, or None.
        ephemeris = get_ephemeris(destination="ephemeris.dat")

    # If ephemeris is still None, then we failed to download the ephemeris data.
    # Try and grab the almanac data instead
    if ephemeris == None:
        logging.error("Could not obtain ephemeris data, trying to download an almanac.")
        almanac = get_almanac(destination="almanac.txt")
        if almanac == None:
            # We probably don't have an internet connection. Bomb out, since we can't do much with the sonde telemetry without an almanac!
            logging.critical("Could not obtain GPS ephemeris or almanac data.")
            return False

    # Add a -T option if bias is enabled
    bias_option = "-T " if bias else ""

    decode_cmd = "rtl_fm %s-p %d -M fm -s 12k -f %d 2>/dev/null |" % (bias_option, int(ppm), frequency)
    decode_cmd += "sox -t raw -r 12k -e s -b 16 -c 1 - -r 48000 -b 8 -t wav - lowpass 2500 highpass 20 2>/dev/null |"

    # Note: I've got the check-CRC option hardcoded in here as always on. 
    # I figure this is prudent if we're going to proceed to push this telemetry data onto a map.

    if ephemeris != None:
        decode_cmd += "./rs92ecc -v --crc --ecc --vel -e %s" % ephemeris
    elif almanac != None:
        decode_cmd += "./rs92ecc -v --crc --ecc --vel -a %s" % almanac

    rx_last_line = time.time()

    # Receiver subprocess. Discard stderr, and feed stdout into an asynchronous read class.
    rx = subprocess.Popen(decode_cmd, shell=True, stdin=None, stdout=subprocess.PIPE, preexec_fn=os.setsid) 
    rx_stdout = AsynchronousFileReader(rx.stdout, autostart=True)

    while not rx_stdout.eof():
        for line in rx_stdout.readlines():
            if (line != None) and (line != ""):
                try:
                    data = process_rs_line(line)
                    # Reset timeout counter.
                    rx_last_line = time.time()

                    if data != None:
                        # Add in a few fields that don't come from the sonde telemetry.
                        data['freq'] = "%.3f MHz" % (frequency/1e6)
                        data['type'] = "RS92"

                        update_flight_stats(data)

                        if rx_queue != None:
                            try:
                                internet_push_queue.put_nowait(data)
                                ozi_push_queue.put_nowait(data)
                            except:
                                pass
                except:
                    traceback.print_exc()
                    logging.error("Error parsing line: %s" % line)

        # Check timeout counter.
        if time.time() > (rx_last_line+timeout):
            logging.error("RX Timed out.")
            break
        # Sleep for a short time.
        time.sleep(0.1)

    logging.error("Closing RX Thread.")
    os.killpg(os.getpgid(rx.pid), signal.SIGTERM)
    rx_stdout.stop()
    rx_stdout.join()
    return


def decode_rs41(frequency, ppm=0, gain='automatic', bias=False, rx_queue=None, timeout=120):
    """ Decode a RS41 sonde """
    global latest_sonde_data, internet_push_queue, ozi_push_queue
    # Add a -T option if bias is enabled
    bias_option = "-T " if bias else ""

    decode_cmd = "rtl_fm %s-p %d -M fm -s 15k -f %d 2>/dev/null |" % (bias_option, int(ppm), frequency)
    decode_cmd += "sox -t raw -r 15k -e s -b 16 -c 1 - -r 48000 -b 8 -t wav - highpass 20 2>/dev/null |"

    # Note: I've got the check-CRC option hardcoded in here as always on. 
    # I figure this is prudent if we're going to proceed to push this telemetry data onto a map.

    decode_cmd += "./rs41ecc --crc --ecc " # if this doesn't work try -i at the end

    rx_last_line = time.time()

    # Receiver subprocess. Discard stderr, and feed stdout into an asynchronous read class.
    rx = subprocess.Popen(decode_cmd, shell=True, stdin=None, stdout=subprocess.PIPE, preexec_fn=os.setsid) 
    rx_stdout = AsynchronousFileReader(rx.stdout, autostart=True)

    while not rx_stdout.eof():
        for line in rx_stdout.readlines():
            if (line != None) and (line != ""):
                try:
                    data = process_rs_line(line)
                    # Reset timeout counter.
                    rx_last_line = time.time()

                    if data != None:
                        # Add in a few fields that don't come from the sonde telemetry.
                        data['freq'] = "%.3f MHz" % (frequency/1e6)
                        data['type'] = "RS41"

                        update_flight_stats(data)

                        latest_sonde_data = data

                        if rx_queue != None:
                            try:
                                internet_push_queue.put_nowait(data)
                                ozi_push_queue.put_nowait(data)
                            except:
                                pass
                except:
                    traceback.print_exc()
                    logging.error("Error parsing line: %s" % line)

        # Check timeout counter.
        if time.time() > (rx_last_line+timeout):
            logging.error("RX Timed out.")
            break
        # Sleep for a short time.
        time.sleep(0.1)

    logging.error("Closing RX Thread.")
    os.killpg(os.getpgid(rx.pid), signal.SIGTERM)
    rx_stdout.stop()
    rx_stdout.join()
    return

def internet_push_thread(station_config):
    """ Push a frame of sonde data into various internet services (APRS-IS, Habitat) """
    global internet_push_queue, INTERNET_PUSH_RUNNING
    print("Started Internet Push thread.")
    while INTERNET_PUSH_RUNNING:
        data = None
        try:
            # Wait until there is somethign in the queue before trying to process.
            if internet_push_queue.empty():
                time.sleep(1)
                continue
            else:
                # Read in entire contents of queue, and keep the most recent entry.
                while not internet_push_queue.empty():
                    data = internet_push_queue.get()
        except:
            traceback.print_exc()
            continue

        try:
            # Wrap this entire section in a try/except, to catch any data parsing errors.
            # APRS Upload
            if station_config['enable_aprs']:
                # Produce aprs comment, based on user config.
                aprs_comment = station_config['aprs_custom_comment']
                aprs_comment = aprs_comment.replace("<freq>", data['freq'])
                aprs_comment = aprs_comment.replace("<id>", data['id'])
                aprs_comment = aprs_comment.replace("<vel_v>", "%.1fm/s" % data['vel_v'])
                aprs_comment = aprs_comment.replace("<type>", data['type'])

                # Push data to APRS.
                aprs_data = push_balloon_to_aprs(data,object_name=station_config['aprs_object_id'],aprs_comment=aprs_comment,aprsUser=station_config['aprs_user'], aprsPass=station_config['aprs_pass'])
                logging.debug("Data pushed to APRS-IS: %s" % aprs_data)

            # Habitat Upload
            if station_config['enable_habitat']:
                habitat_upload_payload_telemetry(data, payload_callsign=config['payload_callsign'], callsign=config['uploader_callsign'])
        except:
            traceback.print_exc()

        if station_config['synchronous_upload']:
            # Sleep for a second to ensure we don't double upload in the same slot (shouldn't' happen, but anyway...)
            time.sleep(1)

            # Wait until the next valid uplink timeslot.
            # This is determined by waiting until the time since epoch modulus the upload rate is equal to zero.
            # Note that this will result in some odd upload times, due to leap seconds and otherwise, but should
            # result in multiple stations (assuming local timezones are the same, and the stations are synced to NTP)
            # uploading at roughly the same time.
            while int(time.time())%station_config['upload_rate'] != 0:
                time.sleep(0.1)
        else:
            # Otherwise, just sleep.
            time.sleep(station_config['upload_rate'])

    print("Closing thread.")

def ozi_push_thread(station_config):
    """ Push a frame of sonde data into various internet services (APRS-IS, Habitat) """
    global ozi_push_queue, OZI_PUSH_RUNNING
    print("Started OziPlotter Push thread.")
    while OZI_PUSH_RUNNING:
        data = None
        try:
            # Wait until there is somethign in the queue before trying to process.
            if ozi_push_queue.empty():
                time.sleep(1)
                continue
            else:
                # Read in entire contents of queue, and keep the most recent entry.
                while not ozi_push_queue.empty():
                    data = ozi_push_queue.get()
        except:
            traceback.print_exc()
            continue

        try:
            if station_config['ozi_enabled']:
                push_telemetry_to_ozi(data,hostname=station_config['ozi_hostname'], udp_port=station_config['ozi_port'])

            if station_config['payload_summary_enabled']:
                push_payload_summary(data, udp_port=station_config['payload_summary_port'])
        except:
            traceback.print_exc()

        time.sleep(station_config['ozi_update_rate'])

    print("Closing thread.")


if __name__ == "__main__":

    # Setup logging.
    logging.basicConfig(format='%(asctime)s %(levelname)s:%(message)s', filename=datetime.datetime.utcnow().strftime("log/%Y%m%d-%H%M%S.log"), level=logging.DEBUG)
    stdout_format = logging.Formatter('%(asctime)s %(levelname)s:%(message)s')
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(stdout_format)
    logging.getLogger().addHandler(stdout_handler)

    # Command line arguments. 
    parser = argparse.ArgumentParser()
    parser.add_argument("-c" ,"--config", default="station.cfg", help="Receive Station Configuration File")
    parser.add_argument("-f", "--frequency", type=float, default=0.0, help="Sonde Frequency (MHz) (bypass scan step).")
    parser.add_argument("-t", "--timeout", type=int, default=180, help="Stop receiving after X minutes.")
    args = parser.parse_args()

    # Attempt to read in configuration file. Use default config if reading fails.
    config = read_auto_rx_config(args.config)

    print(config)

    # Clean up gain value.
    if config['rtlsdr_gain'] == '0' or config['rtlsdr_gain'] == 0:
        config['rtlsdr_gain'] = 'automatic'
    elif type(config['rtlsdr_gain']) == int:
        config['rtlsdr_gain'] = str(config['rtlsdr_gain'])
    else:
        config['rtlsdr_gain'] = 'automatic'

    timeout_time = time.time() + int(args.timeout)*60

    # Internet push thread object.
    push_thread_1 = None
    push_thread_2 = None

    # Sonde Frequency & Type variables.
    sonde_freq = None
    sonde_type = None

    # If Habitat upload is enabled and we have been provided with listener coords, push our position to habitat
    if config['enable_habitat'] and (config['uploader_lat'] != 0.0) and (config['uploader_lon'] != 0.0):
        uploadListenerPosition(config['uploader_callsign'], config['uploader_lat'], config['uploader_lon'])


    # Main scan & track loop. We keep on doing this until we timeout (i.e. after we expect the sonde to have landed)

    while time.time() < timeout_time or args.timeout == 0:
        # Attempt to detect a sonde on a supplied frequency.
        if args.frequency != 0.0:
            sonde_type = detect_sonde(int(float(args.frequency)*1e6), ppm=config['rtlsdr_ppm'], gain=config['rtlsdr_gain'], bias=config['rtlsdr_bias'])
            if sonde_type != None:
                sonde_freq = int(float(args.frequency)*1e6)
        # If nothing is detected, or we haven't been supplied a frequency, perform a scan.
        if sonde_type == None:
            (sonde_freq, sonde_type) = sonde_search(config, config['search_attempts'])

        # If we *still* haven't detected a sonde... just keep on trying, until we hit our timeout.
        if sonde_type == None:
            continue

        logging.info("Starting decoding of %s on %.3f MHz" % (sonde_type, sonde_freq/1e6))

        # Start both of our internet/ozi push threads.
        if push_thread_1 == None:
            push_thread_1 = Thread(target=internet_push_thread, kwargs={'station_config':config})
            push_thread_1.start()

        if push_thread_2 == None:
            push_thread_2 = Thread(target=ozi_push_thread, kwargs={'station_config':config})
            push_thread_2.start()

        # Start decoding the sonde!
        if sonde_type == "RS92":
            decode_rs92(sonde_freq, ppm=config['rtlsdr_ppm'], gain=config['rtlsdr_gain'], bias=config['rtlsdr_bias'], rx_queue=internet_push_queue, timeout=config['rx_timeout'])
        elif sonde_type == "RS41":
            decode_rs41(sonde_freq, ppm=config['rtlsdr_ppm'], gain=config['rtlsdr_gain'], bias=config['rtlsdr_bias'], rx_queue=internet_push_queue, timeout=config['rx_timeout'])
        else:
            pass

        # Receiver has timed out. Reset sonde type and frequency variables and loop.
        logging.error("Receiver timed out. Re-starting scan.")
        time.sleep(10)
        sonde_type = None
        sonde_freq = None

    logging.info("Exceeded maximum receive time. Exiting.")

    # Write flight statistics to file.
    if flight_stats['last'] != None:
        stats_str = calculate_flight_statistics()
        logging.info(stats_str)

        f = open("last_positions.txt", 'a')
        f.write(stats_str + "\n")
        f.close()

    # Stop the APRS output thread.
    INTERNET_PUSH_RUNNING = False


