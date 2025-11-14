# -----------------------------
# IMPORTS
# -----------------------------
import os
import tempfile
import firebase_admin
from firebase_admin import credentials, storage
import moviepy.editor as mp
import google.generativeai as genai
from flask import Flask, request, send_file
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)

# -----------------------------
# FLASK APP SETUP
# -----------------------------
app = Flask(__name__)

# -----------------------------
# FIREBASE CONFIG
# -----------------------------
# NOTE: In a production environment, you should use environment variables
# to store the service account JSON content or path. For Render, upload
# the JSON file and reference it here, or store its content in an ENV variable.
try:
    cred = credentials.Certificate("timetable-2e38f-firebase-adminsdk-fbsvc-bd0ec6a380.json")  # your service account path
    firebase_admin.initialize_app(cred, {
        'storageBucket': 'timetable-2e38f.firebasestorage.app'
    })
    bucket = storage.bucket()
    logging.info("Firebase initialized successfully.")
except Exception as e:
    logging.error(f"Error initializing Firebase: {e}")
    bucket = None

# -----------------------------
# GEMINI CONFIG
# -----------------------------
# BEST PRACTICE: Use an environment variable for the API key.
# On Render, you would set GEMINI_API_KEY as an environment variable.
try:
    gemini_api_key = os.environ.get("GEMINI_API_KEY", "AIzaSyAEalS76fgNYqQ2kvLl2iGqMGX6xBoodFg")
    genai.configure(api_key=gemini_api_key)
    logging.info("Gemini configured.")
except Exception as e:
    logging.error(f"Error configuring Gemini: {e}")

# -----------------------------
# HELPER FUNCTIONS (adapted for server)
# -----------------------------

@app.before_request
def list_videos_in_firebase():
    """List all videos in Firebase under 'Dictionary/' folder."""
    if not hasattr(app, 'available_words') and bucket:
        try:
            blobs = bucket.list_blobs(prefix="Dictionary/")
            # Extract word from "Dictionary/word.mp4"
            video_names = [b.name.split("/")[-1].replace(".mp4", "") for b in blobs if b.name.endswith('.mp4')]
            app.available_words = video_names
            logging.info(f"Available words loaded: {len(video_names)}")
        except Exception as e:
            logging.error(f"Error listing Firebase videos: {e}")
            app.available_words = []
    return

def find_similar_word(word, available_words):
    """
    Find a word in the dictionary closest in meaning using Gemini.
    """
    word = word.lower()
    if word in available_words:
        return word

    prompt = f"""
You have these available words in a Firebase database: {available_words}.
Given the word '{word}', suggest a single word from the database
that is closest in meaning. If none exists, respond with 'NONE'.
Respond ONLY with the word or NONE.
"""
    try:
        model = genai.GenerativeModel("gemini-2.5-flash")
        response = model.generate_content(prompt)
        suggested = response.text.strip().lower()
        if suggested == "none":
            return None
        return suggested
    except Exception as e:
        logging.error(f"[Gemini error] {e}")
        return None

def get_video_clip(word, target_size=(640, 480)):
    """
    Download the video for a word from Firebase, resize, and return a MoviePy clip.
    Uses tempfile to manage the downloaded file.
    """
    temp_file_path = None
    try:
        blob = bucket.blob(f"Dictionary/{word}.mp4")
        if blob.exists():
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as temp_file:
                temp_file_path = temp_file.name
                blob.download_to_filename(temp_file_path)
            
            clip = mp.VideoFileClip(temp_file_path)
            # Resize to uniform size
            clip_resized = clip.resize(newsize=target_size)
            return clip_resized
        else:
            logging.warning(f"No video found for: {word}")
            return None
    except Exception as e:
        logging.error(f"Error getting video clip for {word}: {e}")
        return None
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            os.remove(temp_file_path) # Clean up downloaded file


# -----------------------------
# FLASK ROUTE
# -----------------------------

@app.route('/generate_video', methods=['GET'])
def generate_video():
    """
    Accepts a sentence, generates the merged sign language video, and returns it.
    Example usage: GET /generate_video?sentence=I am hungry
    """
    sentence = request.args.get('sentence')
    if not sentence:
        return {"error": "Missing 'sentence' parameter."}, 400

    if not bucket or not hasattr(app, 'available_words') or not app.available_words:
        return {"error": "Server not fully initialized or Firebase not reachable."}, 503

    logging.info(f"Processing sentence: '{sentence}'")
    words = sentence.lower().split()

    # Find replacements for words not in Firebase
    final_words = []
    for word in words:
        replacement = find_similar_word(word, app.available_words)
        if replacement:
            final_words.append(replacement)
        else:
            logging.info(f"Skipping '{word}' (no video found)")

    if not final_words:
        return {"error": "Could not find sign videos for any word in the sentence."}, 404

    logging.info(f"Final words to fetch: {final_words}")

    # Download, resize, and merge video clips
    clips = [get_video_clip(word) for word in final_words]
    clips = [clip for clip in clips if clip]  # remove None

    if not clips:
        return {"error": "Failed to retrieve any video clips."}, 500

    # Merge clips and save to a temporary file
    temp_output_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as temp_file:
            temp_output_path = temp_file.name
            final_clip = mp.concatenate_videoclips(clips, method="compose")
            
            # NOTE: Render containers might need specific settings for 'write_videofile'
            final_clip.write_videofile(
                temp_output_path, 
                codec="libx264", 
                audio_codec="aac", 
                logger=None # Suppress MoviePy output for clean logs
            )

        # Return the file as an HTTP response
        return send_file(
            temp_output_path, 
            mimetype='video/mp4', 
            as_attachment=True, 
            download_name="merged_sentence.mp4"
        )
    except Exception as e:
        logging.error(f"Error during video merging or serving: {e}")
        return {"error": f"An error occurred during video generation: {e}"}, 500
    finally:
        # Crucial step: Ensure the temporary file is deleted after sending
        if temp_output_path and os.path.exists(temp_output_path):
            os.remove(temp_output_path)


# -----------------------------
# SERVER STARTUP
# -----------------------------
if __name__ == '__main__':
    # Use 0.0.0.0 and port 10000 or the port specified by Render for production
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=True)