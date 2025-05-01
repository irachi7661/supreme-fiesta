import os
import threading
import time
import subprocess
import requests
import logging
from flask import Flask, render_template, jsonify, url_for, abort

# লগার সেটআপ করুন
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)

# ভিডিওর তথ্য
VIDEO_SOURCES = [
    {
        "id": "video1",
        "url": "https://www.dropbox.com/scl/fi/s8o03il2frnpf6381mn9l/cb1547f1-f2f6-4800-ba60-5ea52c247a60_output.mp4?rlkey=8z838g1hcnjgztvwgumisk30s&raw=1"
    },
    {
        "id": "video2",
        "url": "https://www.dropbox.com/scl/fi/b555nv0bkqj2yd73z1jrd/79cf6f18-77f4-47b8-8b43-a14f6b1e641c_output.mp4?rlkey=xf5y1wdk98eqdte5efmg3t0c4&raw=1"
    }
]

# ভিডিও সংরক্ষণের পাথ
VIDEO_DIR = os.path.join('static', 'videos')
os.makedirs(VIDEO_DIR, exist_ok=True)

# রেজোলিউশন
RESOLUTIONS = ["360p", "480p", "720p"] # উচ্চতা অনুযায়ী
FFMPEG_RESOLUTIONS = {"360p": 360, "480p": 480, "720p": 720}

# ভিডিওর অবস্থা ট্র্যাক করার জন্য
video_status = {} # ফরম্যাট: { "video1": {"original": "path/to/original.mp4", "360p": "path/to/360p.mp4", ...}, ... }
processing_complete = False
processing_error = None

# --- ভিডিও প্রসেসিং ফাংশন ---

def download_video(video_info):
    """ নির্দিষ্ট URL থেকে ভিডিও ডাউনলোড করে """
    video_id = video_info["id"]
    url = video_info["url"]
    original_filename = f"{video_id}_original.mp4"
    output_path = os.path.join(VIDEO_DIR, original_filename)

    if os.path.exists(output_path):
        logging.info(f"ভিডিও '{video_id}' ইতিমধ্যে ডাউনলোড করা আছে: {output_path}")
        video_status[video_id] = {"original": output_path}
        return output_path

    logging.info(f"ভিডিও '{video_id}' ডাউনলোড করা হচ্ছে {url} থেকে...")
    try:
        with requests.get(url, stream=True, timeout=300) as r: # টাইমআউট যোগ করা হয়েছে
            r.raise_for_status()
            with open(output_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        logging.info(f"ভিডিও '{video_id}' সফলভাবে ডাউনলোড হয়েছে: {output_path}")
        video_status[video_id] = {"original": output_path}
        return output_path
    except requests.exceptions.RequestException as e:
        logging.error(f"ভিডিও '{video_id}' ডাউনলোড করতে ব্যর্থ হয়েছে: {e}")
        global processing_error
        processing_error = f"ডাউনলোড ব্যর্থ হয়েছে: {video_id} ({e})"
        if os.path.exists(output_path): # অসম্পূর্ণ ফাইল মুছে ফেলুন
            os.remove(output_path)
        return None
    except Exception as e:
        logging.error(f"ভিডিও '{video_id}' ডাউনলোড করার সময় একটি অপ্রত্যাশিত ত্রুটি ঘটেছে: {e}")
        processing_error = f"অপ্রত্যাশিত ডাউনলোড ত্রুটি: {video_id} ({e})"
        if os.path.exists(output_path):
            os.remove(output_path)
        return None


def convert_video(input_path, video_id, resolution_key):
    """ ffmpeg ব্যবহার করে ভিডিওকে নির্দিষ্ট রেজোলিউশনে রূপান্তর করে """
    if not input_path or not os.path.exists(input_path):
        logging.warning(f"রূপান্তরের জন্য ইনপুট ফাইল '{input_path}' পাওয়া যায়নি।")
        return None

    height = FFMPEG_RESOLUTIONS[resolution_key]
    output_filename = f"{video_id}_{resolution_key}.mp4"
    output_path = os.path.join(VIDEO_DIR, output_filename)

    if os.path.exists(output_path):
        logging.info(f"ভিডিও '{video_id}' ({resolution_key}) ইতিমধ্যে রূপান্তরিত আছে: {output_path}")
        video_status[video_id][resolution_key] = output_path
        return output_path

    logging.info(f"ভিডিও '{video_id}' কে {resolution_key} ({height}p) তে রূপান্তর করা হচ্ছে...")

    # ffmpeg কমান্ড: -vf scale=-2:H ব্যবহার করে অনুপাত ঠিক রেখে উচ্চতা অনুযায়ী স্কেল করে
    # -c:v libx264 ভিডিওর জন্য H.264 কোডেক ব্যবহার করে
    # -crf 23 কোয়ালিটি লেভেল (কম মানে ভালো কোয়ালিটি, বেশি ফাইলের আকার)
    # -preset veryfast এনকোডিং স্পীড (দ্রুত কিন্তু কম কার্যকর)
    # -c:a copy অডিও স্ট্রিম পুনরায় এনকোড না করে কপি করে (দ্রুত এবং কোয়ালিটি অপরিবর্তিত)
    # -movflags +faststart ওয়েব প্লেব্যাকের জন্য অপ্টিমাইজ করে
    command = [
        'ffmpeg',
        '-i', input_path,
        '-vf', f'scale=-2:{height}',
        '-c:v', 'libx264',
        '-crf', '23',
        '-preset', 'veryfast',
        '-c:a', 'copy',
        '-movflags', '+faststart',
        output_path
    ]

    try:
        # ffmpeg কমান্ড চালান
        process = subprocess.run(command, check=True, capture_output=True, text=True)
        logging.info(f"ভিডিও '{video_id}' ({resolution_key}) সফলভাবে রূপান্তরিত হয়েছে: {output_path}")
        video_status[video_id][resolution_key] = output_path
        return output_path
    except FileNotFoundError:
        logging.error("ত্রুটি: 'ffmpeg' কমান্ড পাওয়া যায়নি। এটি ইনস্টল করা আছে এবং PATH এ আছে কিনা তা নিশ্চিত করুন।")
        global processing_error
        processing_error = "'ffmpeg' পাওয়া যায়নি। এটি ইনস্টল করা প্রয়োজন।"
        return None
    except subprocess.CalledProcessError as e:
        logging.error(f"ভিডিও '{video_id}' ({resolution_key}) রূপান্তর করতে ব্যর্থ হয়েছে।")
        logging.error(f"FFmpeg Error Output:\n{e.stderr}")
        processing_error = f"'{video_id}' ({resolution_key}) রূপান্তর ব্যর্থ হয়েছে।"
        if os.path.exists(output_path): # ব্যর্থ হলে অসম্পূর্ণ ফাইল মুছে ফেলুন
            os.remove(output_path)
        return None
    except Exception as e:
        logging.error(f"ভিডিও '{video_id}' ({resolution_key}) রূপান্তর করার সময় একটি অপ্রত্যাশিত ত্রুটি ঘটেছে: {e}")
        processing_error = f"অপ্রত্যাশিত রূপান্তর ত্রুটি: {video_id} ({resolution_key}) ({e})"
        if os.path.exists(output_path):
            os.remove(output_path)
        return None


def process_all_videos():
    """ সমস্ত ভিডিও ডাউনলোড এবং রূপান্তর করার জন্য ব্যাকগ্রাউন্ড টাস্ক """
    global processing_complete, processing_error
    processing_error = None # শুরু করার আগে আগের ত্রুটি মুছে ফেলুন
    logging.info("ভিডিও প্রসেসিং শুরু হচ্ছে...")

    try:
        for video_info in VIDEO_SOURCES:
            video_id = video_info["id"]
            # 1. ডাউনলোড করুন (যদি প্রয়োজন হয়)
            original_path = download_video(video_info)
            if not original_path:
                logging.error(f"'{video_id}' এর ডাউনলোড ব্যর্থ হওয়ায় রূপান্তর করা সম্ভব নয়।")
                continue # পরবর্তী ভিডিওতে যান

            if video_id not in video_status:
                 video_status[video_id] = {}
            video_status[video_id]['original'] = original_path


            # 2. রূপান্তর করুন (যদি প্রয়োজন হয়)
            for res_key in RESOLUTIONS:
                if processing_error: # যদি কোনো ধাপে ত্রুটি ঘটে তবে প্রসেসিং বন্ধ করুন
                     raise Exception(f"প্রসেসিং বন্ধ করা হয়েছে পূর্ববর্তী ত্রুটির কারণে: {processing_error}")
                convert_video(original_path, video_id, res_key)
                # ছোট বিরতি দেওয়া যেতে পারে সিস্টেম লোড কমাতে
                # time.sleep(1)

        # চেক করুন সব রেজোলিউশন তৈরি হয়েছে কিনা
        all_converted = True
        for video_id in video_status:
             if 'original' not in video_status[video_id]:
                 all_converted = False
                 break
             for res_key in RESOLUTIONS:
                 if res_key not in video_status[video_id]:
                     logging.warning(f"ভিডিও '{video_id}' এর জন্য রেজোলিউশন '{res_key}' তৈরি হয়নি।")
                     all_converted = False
                     # এখানে আপনি চাইলে আবার চেষ্টা করতে পারেন বা শুধু লগ করতে পারেন

        if all_converted and not processing_error:
             processing_complete = True
             logging.info("সমস্ত ভিডিও সফলভাবে প্রসেস করা হয়েছে।")
        elif not processing_error:
             processing_error = "কিছু ভিডিওর রেজোলিউশন তৈরি করা যায়নি।" # একটি সাধারণ ত্রুটি বার্তা সেট করুন যদি কোনো নির্দিষ্ট ত্রুটি না থাকে
             logging.warning("কিছু ভিডিওর রেজোলিউশন তৈরি করা যায়নি। লগ চেক করুন।")


    except Exception as e:
         logging.error(f"ভিডিও প্রসেসিং এর সময় একটি ত্রুটি ঘটেছে: {e}")
         if not processing_error: # যদি কোনো নির্দিষ্ট ত্রুটি আগে সেট না হয়ে থাকে
             processing_error = f"ভিডিও প্রসেসিং ব্যর্থ হয়েছে: {e}"

    if processing_error:
         logging.error(f"চূড়ান্ত প্রসেসিং ত্রুটি: {processing_error}")


# --- ফ্লাস্ক রুট ---

@app.route('/')
def index():
    """ মূল HTML পৃষ্ঠা রেন্ডার করে """
    # ভিডিওর পাথগুলি টেমপ্লেটে পাস করার জন্য প্রস্তুত করুন
    video_data = {}
    if processing_complete:
        for video_id, paths in video_status.items():
            video_data[video_id] = {}
            for res_key, path in paths.items():
                if res_key != 'original': # শুধুমাত্র রেজোলিউশন পাথগুলি পাঠান
                    # url_for ব্যবহার করে সঠিক URL তৈরি করুন
                    video_data[video_id][res_key] = url_for('static', filename=os.path.join('videos', os.path.basename(path)))
                    # video_data[video_id][res_key] = f"/static/videos/{os.path.basename(path)}" # বিকল্প উপায়

    return render_template('index.html',
                           video_data=video_data,
                           video_ids=list(video_status.keys()), # ভিডিও আইডিগুলির একটি তালিকা পাঠান
                           resolutions=RESOLUTIONS,
                           processing_complete=processing_complete,
                           processing_error=processing_error)

@app.route('/status')
def status():
    """ প্রসেসিংয়ের অবস্থা জানানোর জন্য একটি API এন্ডপয়েন্ট """
    return jsonify({
        "processing_complete": processing_complete,
        "processing_error": processing_error,
        "available_videos": video_status # বিস্তারিত অবস্থা দেখতে চাইলে এটি ব্যবহার করা যেতে পারে
    })

# --- অ্যাপ্লিকেশন শুরু এবং ব্যাকগ্রাউন্ড থ্রেড ---

if __name__ == '__main__':
    # নিশ্চিত করুন VIDEO_DIR বিদ্যমান
    os.makedirs(VIDEO_DIR, exist_ok=True)

    # ব্যাকগ্রাউন্ডে ভিডিও প্রসেসিং শুরু করুন
    processing_thread = threading.Thread(target=process_all_videos, daemon=True)
    processing_thread.start()

    # ফ্লাস্ক অ্যাপ্লিকেশন চালান (ডিবাগ মোড বন্ধ রাখুন প্রোডাকশনে)
    # app.run(debug=True, host='0.0.0.0') # ডিবাগিং এর জন্য
    app.run(host='0.0.0.0', port=5000) # ডকারের জন্য
                                 
