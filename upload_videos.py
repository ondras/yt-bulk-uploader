#!/usr/bin/env python3
"""Dávkový upload videí na YouTube.

Projde zadanou složku, nahraje všechna videa jako "unlisted" a pro každé
vytvoří vedle něj soubor .url s odkazem na detail videa na YouTube.
Videa, ke kterým už .url existuje, přeskočí (idempotentní opakované spuštění).

Nastavení (jednorázově):
  1. Google Cloud Console -> nový projekt.
  2. Povolit "YouTube Data API v3".
  3. OAuth consent screen (External, přidat sebe jako test usera).
  4. Vytvořit OAuth client ID typu "Desktop app", stáhnout client_secrets.json.
  5. pip install google-api-python-client google-auth-oauthlib google-auth-httplib2

Pozor na kvóty: jeden upload stojí ~1600 jednotek, denní limit je ~10000,
takže reálně cca 6 videí/den (pokud nemáte navýšenou kvótu). Skript je
navržen tak, aby se dal bezpečně spustit znovu po vyčerpání kvóty.
"""

import argparse
import http.client
import os
import random
import sys
import time

import googleapiclient.errors
import googleapiclient.http
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
API_SERVICE_NAME = "youtube"
API_VERSION = "v3"

VIDEO_EXTENSIONS = {
	".mp4", ".mov", ".avi", ".mkv", ".m4v", ".wmv", ".flv", ".webm",
}

# Chyby, u kterých má smysl to zkusit znovu.
RETRIABLE_EXCEPTIONS = (
	http.client.NotConnected,
	http.client.IncompleteRead,
	http.client.ImproperConnectionState,
	http.client.CannotSendRequest,
	http.client.CannotSendHeader,
	http.client.ResponseNotReady,
	http.client.BadStatusLine,
	ConnectionError,
	OSError,
)
RETRIABLE_STATUS_CODES = {500, 502, 503, 504}
MAX_RETRIES = 10

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def get_authenticated_service(secrets_path, token_path):
	creds = None
	if os.path.exists(token_path):
		creds = Credentials.from_authorized_user_file(token_path, SCOPES)

	if not creds or not creds.valid:
		if creds and creds.expired and creds.refresh_token:
			creds.refresh(Request())
		else:
			if not os.path.exists(secrets_path):
				sys.exit(f"Nenalezen soubor s přihlašovacími údaji: {secrets_path}")
			flow = InstalledAppFlow.from_client_secrets_file(secrets_path, SCOPES)
			creds = flow.run_local_server(port=0)
		with open(token_path, "w", encoding="utf-8") as f:
			f.write(creds.to_json())

	return build(API_SERVICE_NAME, API_VERSION, credentials=creds)


def find_videos(folder):
	entries = []
	for name in os.listdir(folder):
		path = os.path.join(folder, name)
		if not os.path.isfile(path):
			continue
		if os.path.splitext(name)[1].lower() in VIDEO_EXTENSIONS:
			entries.append(path)
	entries.sort()
	return entries


def url_path_for(video_path):
	base, _ = os.path.splitext(video_path)
	return base + ".url"


def write_url_file(video_path, video_id):
	url = f"https://www.youtube.com/watch?v={video_id}"
	content = f"[InternetShortcut]\nURL={url}\n"
	with open(url_path_for(video_path), "w", encoding="utf-8") as f:
		f.write(content)


def upload_video(youtube, path, privacy):
	title = os.path.splitext(os.path.basename(path))[0]
	body = {
		"snippet": {
			"title": title,
			"description": "",
			"categoryId": "22",
		},
		"status": {
			"privacyStatus": privacy,
		},
	}

	media = googleapiclient.http.MediaFileUpload(path, chunksize=-1, resumable=True)
	request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

	response = None
	error = None
	retry = 0
	while response is None:
		try:
			status, response = request.next_chunk()
			if status:
				print(f"    …{int(status.progress() * 100)} %")
			if response is not None:
				if "id" in response:
					return response["id"]
				raise RuntimeError(f"Neočekávaná odpověď od API: {response}")
		except googleapiclient.errors.HttpError as e:
			if e.resp.status in RETRIABLE_STATUS_CODES:
				error = f"HTTP {e.resp.status}: {e.content}"
			elif e.resp.status == 403 and b"quota" in (e.content or b"").lower():
				sys.exit(
					"Vyčerpána denní kvóta YouTube API (quotaExceeded). "
					"Spusťte skript znovu později – již nahraná videa se přeskočí."
				)
			else:
				raise
		except RETRIABLE_EXCEPTIONS as e:
			error = str(e)

		if error is not None:
			retry += 1
			if retry > MAX_RETRIES:
				sys.exit(f"Příliš mnoho chyb, končím. Poslední chyba: {error}")
			sleep = random.random() * (2 ** retry)
			print(f"    Chyba ({error}); zkouším znovu za {sleep:.1f} s…")
			time.sleep(sleep)
			error = None


def main():
	parser = argparse.ArgumentParser(
		description="Dávkově nahraje videa ze složky na YouTube a vytvoří .url odkazy."
	)
	parser.add_argument("folder", help="Složka s videi.")
	parser.add_argument(
		"--secrets",
		default=os.path.join(SCRIPT_DIR, "client_secrets.json"),
		help="Cesta ke client_secrets.json (výchozí: vedle skriptu).",
	)
	parser.add_argument(
		"--token",
		default=os.path.join(SCRIPT_DIR, "token.json"),
		help="Cesta k uloženému tokenu (výchozí: vedle skriptu).",
	)
	parser.add_argument(
		"--privacy",
		default="unlisted",
		choices=["unlisted", "private", "public"],
		help="Viditelnost nahraných videí (výchozí: unlisted).",
	)
	args = parser.parse_args()

	if not os.path.isdir(args.folder):
		sys.exit(f"Složka neexistuje: {args.folder}")

	videos = find_videos(args.folder)
	if not videos:
		print("Ve složce nejsou žádná videa.")
		return

	youtube = get_authenticated_service(args.secrets, args.token)

	uploaded = skipped = failed = 0
	for path in videos:
		name = os.path.basename(path)
		if os.path.exists(url_path_for(path)):
			print(f"[skip]   {name} (.url už existuje)")
			skipped += 1
			continue

		print(f"[upload] {name}")
		try:
			video_id = upload_video(youtube, path, args.privacy)
		except SystemExit:
			raise
		except Exception as e:
			print(f"    CHYBA: {e}")
			failed += 1
			continue

		write_url_file(path, video_id)
		print(f"    hotovo -> https://www.youtube.com/watch?v={video_id}")
		uploaded += 1

	print(f"\nSouhrn: nahráno {uploaded}, přeskočeno {skipped}, chyb {failed}.")


if __name__ == "__main__":
	main()
