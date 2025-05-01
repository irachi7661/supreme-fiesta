# Base image হিসাবে Python 3.9 ব্যবহার করুন
FROM python:3.9-slim

# ওয়ার্কিং ডিরেক্টরি সেট করুন
WORKDIR /app

# ffmpeg ইনস্টল করুন (ভিডিও রূপান্তরের জন্য)
# RUN apt-get update এই লাইনটি সরিয়ে দিন যদি আপনার নেটওয়ার্ক প্রক্সি ব্যবহার করে অথবা ইন্টারনেট অ্যাক্সেস সীমাবদ্ধ থাকে
# RUN apt-get update && apt-get install -y ffmpeg --no-install-recommends && rm -rf /var/lib/apt/lists/*
# উপরের লাইনটি ব্যবহার করতে সমস্যা হলে, একটি মাল্টি-স্টেজ বিল্ড ব্যবহার করতে পারেন বা নিশ্চিত করুন যে আপনার ডকার পরিবেশে ইন্টারনেট অ্যাক্সেস আছে।
# আপাতত, ধরে নিচ্ছি ffmpeg হোস্ট সিস্টেমে আছে বা অন্য কোনো উপায়ে কন্টেইনারে পাওয়া যাবে।
# একটি বিকল্প হতে পারে ffmpeg এর স্ট্যাটিক বিল্ড ডাউনলোড করা।
# বিকল্প ffmpeg ইনস্টলেশন (যদি apt-get কাজ না করে বা আপনি একটি ভিন্ন পদ্ধতি চান):
# RUN apt-get update && apt-get install -y wget && \
#     wget https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz && \
#     tar -xf ffmpeg-release-amd64-static.tar.xz && \
#     mv ffmpeg-*-static/ffmpeg /usr/local/bin/ && \
#     rm -rf ffmpeg-* && \
#     apt-get remove -y wget && apt-get clean && rm -rf /var/lib/apt/lists/*

# উপরের ffmpeg ইনস্টলেশন অংশটি আপনার পরিবেশের জন্য উপযুক্তভাবে পরিবর্তন করুন।
# যদি ডকার বিল্ডের সময় ffmpeg ইনস্টল করতে ব্যর্থ হন, তাহলে আপনাকে এটি ম্যানুয়ালি কন্টেইনারে যোগ করতে হবে বা একটি ভিন্ন বেস ইমেজ ব্যবহার করতে হবে যাতে ffmpeg অন্তর্ভুক্ত থাকে।
# সহজ করার জন্য, এই উদাহরণে ধরে নিচ্ছি ffmpeg পাওয়া যাবে। আপনার সিস্টেমে এটি ইনস্টল করা নিশ্চিত করুন।

# requirements.txt কপি করুন এবং নির্ভরতা ইনস্টল করুন
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# অ্যাপ্লিকেশন কোড কপি করুন
COPY . .

# ভিডিও সংরক্ষণের জন্য ডিরেক্টরি তৈরি করুন
RUN mkdir -p static/videos

# অ্যাপ্লিকেশন চালানোর জন্য পোর্ট এক্সপোজ করুন
EXPOSE 5000

# অ্যাপ্লিকেশন চালানোর কমান্ড
# ডিবাগিংয়ের জন্য ফ্লাস্ক ডেভেলপমেন্ট সার্ভার ব্যবহার করুন
# প্রোডাকশনের জন্য Gunicorn বা uWSGI ব্যবহার করার পরামর্শ দেওয়া হচ্ছে
ENV FLASK_APP=app.py
ENV FLASK_RUN_HOST=0.0.0.0
CMD ["flask", "run"]

# প্রোডাকশনের জন্য Gunicorn ব্যবহার করতে হলে:
# RUN pip install gunicorn
# CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]
