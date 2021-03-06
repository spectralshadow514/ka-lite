import json
import re
import requests
from annoying.functions import get_object_or_None
from functools import partial
from requests.exceptions import ConnectionError, HTTPError

from django.core.exceptions import ValidationError, PermissionDenied
from django.db.models import Q
from django.http import HttpResponse
from django.utils import simplejson
from django.utils.translation import ugettext as _

import settings
from .api_forms import ExerciseLogForm, VideoLogForm
from .models import FacilityUser, VideoLog, ExerciseLog, VideoFile
from config.models import Settings
from main import topicdata  # must import this way to cache across processes
from securesync.models import FacilityGroup
from shared.caching import invalidate_all_pages_related_to_video
from shared.decorators import require_admin
from shared.jobs import force_job, job_status
from shared.videos import delete_downloaded_files
from utils.general import break_into_chunks
from utils.internet import api_handle_error_with_json, JsonResponse
from utils.mplayer_launcher import play_video_in_new_thread
from utils.orderedset import OrderedSet


class student_log_api(object):
    def __init__(self, logged_out_message):
        self.logged_out_message = logged_out_message

    def __call__(self, handler):
        @api_handle_error_with_json
        def wrapper_fn(request, *args, **kwargs):
            # TODO(bcipolli): send user info in the post data,
            #   allowing cross-checking of user information
            #   and better error reporting
            if "facility_user" not in request.session:
                return JsonResponse({"warning": self.logged_out_message + "  " + _("You must be logged in as a student or teacher to view/save progress.")}, status=500)
            else:
                return handler(request)
        return wrapper_fn


@student_log_api(logged_out_message=_("Video progress not saved."))
def save_video_log(request):
    """
    Receives a youtube_id and relevant data,
    saves it to the currently authorized user.
    """

    # Form does all the data validation, including the youtube_id
    form = VideoLogForm(data=simplejson.loads(request.raw_post_data))
    if not form.is_valid():
        raise ValidationError(form.errors)
    data = form.data

    try:
        videolog = VideoLog.update_video_log(
            facility_user=request.session["facility_user"],
            youtube_id=data["youtube_id"],
            total_seconds_watched=data["total_seconds_watched"],  # don't set incrementally, to avoid concurrency issues
            points=data["points"],
        )
    except ValidationError as e:
        return JsonResponse({"error": "Could not save VideoLog: %s" % e}, status=500)

    return JsonResponse({
        "points": videolog.points,
        "complete": videolog.complete,
        "messages": {},
    })


@student_log_api(logged_out_message=_("Exercise progress not saved."))
def save_exercise_log(request):
    """
    Receives an exercise_id and relevant data,
    saves it to the currently authorized user.
    """

    # Form does all data validation, including of the exercise_id
    form = ExerciseLogForm(data=simplejson.loads(request.raw_post_data))
    if not form.is_valid():
        raise Exception(form.errors)
    data = form.data

    # More robust extraction of previous object
    (exerciselog, was_created) = ExerciseLog.get_or_initialize(user=request.session["facility_user"], exercise_id=data["exercise_id"])
    previously_complete = exerciselog.complete


    exerciselog.attempts = data["attempts"]  # don't increment, because we fail to save some requests
    exerciselog.streak_progress = data["streak_progress"]
    exerciselog.points = data["points"]

    try:
        exerciselog.full_clean()
        exerciselog.save()
    except ValidationError as e:
        return JsonResponse({"error": _("Could not save ExerciseLog") + u": %s" % e}, status=500)

    # Special message if you've just completed.
    #   NOTE: it's important to check this AFTER calling save() above.
    if not previously_complete and exerciselog.complete:
        return JsonResponse({"success": _("You have mastered this exercise!")})

    # Return no message in release mode; "data saved" message in debug mode.
    return JsonResponse({})


@student_log_api(logged_out_message=_("Progress not loaded."))
def get_video_logs(request):
    """
    Given a list of youtube_ids, retrieve a list of video logs for this user.
    """

    data = simplejson.loads(request.raw_post_data or "[]")
    if not isinstance(data, list):
        return JsonResponse({"error": "Could not load VideoLog objects: Unrecognized input data format." % e}, status=500)

    user = request.session["facility_user"]
    responses = []
    for youtube_id in data:
        response = _get_video_log_dict(request, user, youtube_id)
        if response:
            responses.append(response)
    return JsonResponse(responses)


def _get_video_log_dict(request, user, youtube_id):
    if not youtube_id:
        return {}
    try:
        videolog = VideoLog.objects.filter(user=user, youtube_id=youtube_id).latest("counter")
    except VideoLog.DoesNotExist:
        return {}
    return {
        "youtube_id": youtube_id,
        "total_seconds_watched": videolog.total_seconds_watched,
        "complete": videolog.complete,
        "points": videolog.points,
    }


@student_log_api(logged_out_message=_("Progress not loaded."))
def get_exercise_logs(request):
    """
    Given a list of exercise_ids, retrieve a list of video logs for this user.
    """

    data = simplejson.loads(request.raw_post_data or "[]")
    if not isinstance(data, list):
        return JsonResponse({"error": "Could not load ExerciseLog objects: Unrecognized input data format." % e}, status=500)

    user = request.session["facility_user"]

    return JsonResponse(
        list(ExerciseLog.objects \
            .filter(user=user, exercise_id__in=data) \
            .values("exercise_id", "streak_progress", "complete", "points", "struggling", "attempts"))
    )

@require_admin
@api_handle_error_with_json
def start_video_download(request):
    youtube_ids = OrderedSet(simplejson.loads(request.raw_post_data or "{}").get("youtube_ids", []))

    video_files_to_create = [id for id in youtube_ids if not get_object_or_None(VideoFile, youtube_id=id)]
    video_files_to_update = youtube_ids - OrderedSet(video_files_to_create)

    VideoFile.objects.bulk_create([VideoFile(youtube_id=id, flagged_for_download=True) for id in video_files_to_create])

    for chunk in break_into_chunks(youtube_ids):
        video_files_needing_model_update = VideoFile.objects.filter(download_in_progress=False, youtube_id__in=chunk).exclude(percent_complete=100)
        video_files_needing_model_update.update(percent_complete=0, cancel_download=False, flagged_for_download=True)

    force_job("videodownload", "Download Videos")
    return JsonResponse({})


@require_admin
@api_handle_error_with_json
def retry_video_download(request):
    """Clear any video still accidentally marked as in-progress, and restart the download job.
    """
    VideoFile.objects.filter(download_in_progress=True).update(download_in_progress=False, percent_complete=0)
    force_job("videodownload", "Download Videos")
    return JsonResponse({})


@require_admin
@api_handle_error_with_json
def delete_videos(request):
    youtube_ids = simplejson.loads(request.raw_post_data or "{}").get("youtube_ids", [])
    for id in youtube_ids:
        # Delete the file on disk
        delete_downloaded_files(id)

        # Delete the file in the database
        videofile = get_object_or_None(VideoFile, youtube_id=id)
        if videofile:
            videofile.cancel_download = True
            videofile.flagged_for_download = False
            videofile.flagged_for_subtitle_download = False
            videofile.save()

        # Refresh the cache
        invalidate_all_pages_related_to_video(video_id=id)

    return JsonResponse({})


@require_admin
@api_handle_error_with_json
def check_video_download(request):
    youtube_ids = simplejson.loads(request.raw_post_data or "{}").get("youtube_ids", [])
    percentages = {}
    percentages["downloading"] = job_status("videodownload")
    for id in youtube_ids:
        videofile = get_object_or_None(VideoFile, youtube_id=id)
        percentages[id] = videofile.percent_complete if videofile else None
    return JsonResponse(percentages)


@require_admin
@api_handle_error_with_json
def get_video_download_list(request):
    videofiles = VideoFile.objects.filter(flagged_for_download=True).values("youtube_id")
    video_ids = [video["youtube_id"] for video in videofiles]
    return JsonResponse(video_ids)


@require_admin
@api_handle_error_with_json
def get_video_download_status(request):
    """Get info about what video is currently being downloaded, and how many are left.
    So far, this is only used for debugging, but could be a more robust alternative to
    `check_video_download` if we change our approach slightly.
    """

    videofile = get_object_or_None(VideoFile, download_in_progress=True)
    return JsonResponse({
        "waiting_count": VideoFile.objects.filter(flagged_for_download=True).count(),
        "current_video_id": videofile.youtube_id if videofile else None,
        "current_video_percent": videofile.percent_complete if videofile else 0,
        "downloading": job_status("videodownload"),
    })


@require_admin
@api_handle_error_with_json
def start_subtitle_download(request):
    update_set = simplejson.loads(request.raw_post_data or "{}").get("update_set", "existing")
    language = simplejson.loads(request.raw_post_data or "{}").get("language", "")

    # Set subtitle language
    Settings.set("subtitle_language", language)

    # Get the json file with all srts
    request_url = "http://%s/static/data/subtitles/languages/%s_available_srts.json" % (settings.CENTRAL_SERVER_HOST, language)
    try:
        r = requests.get(request_url)
        r.raise_for_status() # will return none if 200, otherwise will raise HTTP error
        available_srts = set((r.json)["srt_files"])
    except ConnectionError:
        return JsonResponse({"error": "The central server is currently offline."}, status=500)
    except HTTPError:
        return JsonResponse({"error": "No subtitles available on central server for language code: %s; aborting." % language}, status=500)

    if update_set == "existing":
        videofiles = VideoFile.objects.filter(subtitles_downloaded=False, subtitle_download_in_progress=False)
    else:
        videofiles = VideoFile.objects.filter(subtitle_download_in_progress=False)

    queue_count = 0
    for chunk in break_into_chunks(available_srts):
        queue_count += videofiles.filter(youtube_id__in=chunk).update(flagged_for_subtitle_download=True, subtitles_downloaded=False)

    if queue_count == 0:
        return JsonResponse({"info": "There aren't any subtitles available in this language for your currently downloaded videos."}, status=200)
        
    force_job("subtitledownload", "Download Subtitles")
    return JsonResponse({})


@require_admin
@api_handle_error_with_json
def check_subtitle_download(request):
    videofiles = VideoFile.objects.filter(flagged_for_subtitle_download=True)
    return JsonResponse(videofiles.count())

@require_admin
@api_handle_error_with_json
def get_subtitle_download_list(request):
    videofiles = VideoFile.objects.filter(flagged_for_subtitle_download=True).values("youtube_id")
    video_ids = [video["youtube_id"] for video in videofiles]
    return JsonResponse(video_ids)

@require_admin
@api_handle_error_with_json
def cancel_downloads(request):

    # clear all download in progress flags, to make sure new downloads will go through
    VideoFile.objects.all().update(download_in_progress=False)

    # unflag all video downloads
    VideoFile.objects.filter(flagged_for_download=True).update(cancel_download=True, flagged_for_download=False)

    # unflag all subtitle downloads
    VideoFile.objects.filter(flagged_for_subtitle_download=True).update(cancel_download=True, flagged_for_subtitle_download=False)

    force_job("videodownload", stop=True)
    force_job("subtitledownload", stop=True)

    return JsonResponse({})


@require_admin
@api_handle_error_with_json
def remove_from_group(request):
    users = simplejson.loads(request.raw_post_data or "{}").get("users", "")
    users_to_remove = FacilityUser.objects.filter(username__in=users)
    users_to_remove.update(group=None)
    return JsonResponse({})

@require_admin
@api_handle_error_with_json
def move_to_group(request):
    users = simplejson.loads(request.raw_post_data or "{}").get("users", [])
    group = simplejson.loads(request.raw_post_data or "{}").get("group", "")
    group_update = FacilityGroup.objects.get(pk=group)
    users_to_move = FacilityUser.objects.filter(username__in=users)
    users_to_move.update(group=group_update)
    return JsonResponse({})

@require_admin
@api_handle_error_with_json
def delete_users(request):
    users = simplejson.loads(request.raw_post_data or "{}").get("users", [])
    users_to_delete = FacilityUser.objects.filter(username__in=users)
    users_to_delete.delete()
    return JsonResponse({})

def annotate_topic_tree(node, level=0, statusdict=None):
    if not statusdict:
        statusdict = {}
    if node["kind"] == "Topic":
        if "Video" not in node["contains"]:
            return None
        children = []
        unstarted = True
        complete = True
        for child_node in node["children"]:
            child = annotate_topic_tree(child_node, level=level+1, statusdict=statusdict)
            if child:
                if child["addClass"] == "unstarted":
                    complete = False
                if child["addClass"] == "partial":
                    complete = False
                    unstarted = False
                if child["addClass"] == "complete":
                    unstarted = False
                children.append(child)
        return {
            "title": node["title"],
            "tooltip": re.sub(r'<[^>]*?>', '', node["description"] or ""),
            "isFolder": True,
            "key": node["slug"],
            "children": children,
            "addClass": complete and "complete" or unstarted and "unstarted" or "partial",
            "expand": level < 1,
        }
    if node["kind"] == "Video":
        #statusdict contains an item for each video registered in the database
        # will be {} (empty dict) if there are no videos downloaded yet
        percent = statusdict.get(node["youtube_id"], 0)
        if not percent:
            status = "unstarted"
        elif percent == 100:
            status = "complete"
        else:
            status = "partial"
        return {
            "title": node["title"],
            "tooltip": re.sub(r'<[^>]*?>', '', node["description"] or ""),
            "key": node["youtube_id"],
            "addClass": status,
        }
    return None

#@require_admin
def get_annotated_topic_tree():
    statusdict = dict(VideoFile.objects.values_list("youtube_id", "percent_complete"))
    return annotate_topic_tree(topicdata.TOPICS, statusdict=statusdict)

@require_admin
@api_handle_error_with_json
def get_topic_tree(request):
    return JsonResponse(get_annotated_topic_tree())

@api_handle_error_with_json
def launch_mplayer(request):
    """Launch an mplayer instance in a new thread, to play the video requested via the API.
    """
    
    if not settings.USE_MPLAYER:
        raise PermissionDenied("You can only initiate mplayer if USE_MPLAYER is set to True.")
    
    if "youtube_id" not in request.REQUEST:
        return JsonResponse({"error": "no youtube_id specified"}, status=500)

    youtube_id = request.REQUEST["youtube_id"]
    facility_user = request.session.get("facility_user")
    callback = partial(
        _update_video_log_with_points,
        youtube_id=youtube_id,
        facility_user=facility_user,
    )
    play_video_in_new_thread(youtube_id, callback=callback)

    return JsonResponse({})


def _update_video_log_with_points(seconds_watched, video_length, youtube_id, facility_user):
    """Handle the callback from the mplayer thread, saving the VideoLog. """
    
    if not facility_user:
        return  # in other places, we signal to the user that info isn't being saved, but can't do it here.
                #   adding this code for consistency / documentation purposes.

    new_points = (float(seconds_watched) / video_length) * VideoLog.POINTS_PER_VIDEO
    
    videolog = VideoLog.update_video_log(
        facility_user=facility_user,
        youtube_id=youtube_id,
        additional_seconds_watched=seconds_watched,
        new_points=new_points,
    )
