"""The route table: METHOD + path pattern -> handler(req).

ROUTES holds (method, compiled regex, handler, pattern). {id} captures a number from the URL. To add an endpoint, write a
handler(req) in the matching module and add one line here.
"""

from __future__ import annotations

import re

from . import account, friends, lifts, profile, programs, progress, reference


def _compile(pattern: str) -> re.Pattern:
    return re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>\\d+)", pattern) + "/?$")


_ROUTES = [
    # Profile
    ("GET",    "/me",                         profile.get_me),
    ("PUT",    "/me",                         profile.update_me),
    ("GET",    "/me/export",                  account.export_me),
    ("DELETE", "/me",                         account.delete_me),
    # Reference data
    ("GET",    "/muscle-groups",              reference.list_muscle_groups),
    ("GET",    "/exercises",                  reference.list_exercises),
    ("POST",   "/exercises",                  reference.create_exercise),
    # Lifts
    ("GET",    "/lifts",                      lifts.list_my_lifts),
    ("POST",   "/lifts",                      lifts.create_lift),
    ("GET",    "/lifts/{id}",                 lifts.get_lift),
    ("PUT",    "/lifts/{id}",                 lifts.update_lift),
    ("DELETE", "/lifts/{id}",                 lifts.delete_lift),
    ("POST",   "/lifts/{id}/sets",            lifts.add_set_to_lift),
    ("POST",   "/lifts/{id}/apply",           lifts.apply_program_day),
    ("PUT",    "/sets/{id}",                  lifts.update_set),
    ("DELETE", "/sets/{id}",                  lifts.delete_set),
    ("POST",   "/sets/{id}/subsets",          lifts.add_subset_to_set),
    ("PUT",    "/subsets/{id}",               lifts.update_subset),
    ("DELETE", "/subsets/{id}",               lifts.delete_subset),
    # Progress
    ("GET",    "/progress/body-weight",       progress.progress_body_weight),
    ("GET",    "/progress/strength",          progress.progress_strength),
    # Programs
    ("GET",    "/programs",                   programs.list_programs),
    ("POST",   "/programs",                   programs.create_program),
    ("GET",    "/programs/{id}",              programs.get_program),
    ("PUT",    "/programs/{id}",              programs.update_program),
    ("DELETE", "/programs/{id}",              programs.delete_program),
    ("POST",   "/programs/{id}/days",         programs.add_program_day),
    ("PUT",    "/program-days/{id}",          programs.update_program_day),
    ("DELETE", "/program-days/{id}",          programs.delete_program_day),
    ("POST",   "/program-days/{id}/sets",     programs.add_program_set),
    ("PUT",    "/program-sets/{id}",          programs.update_program_set),
    ("DELETE", "/program-sets/{id}",          programs.delete_program_set),
    ("POST",   "/program-sets/{id}/subsets",  programs.add_program_subset),
    ("PUT",    "/program-subsets/{id}",       programs.update_program_subset),
    ("DELETE", "/program-subsets/{id}",       programs.delete_program_subset),
    # Friends
    ("GET",    "/friends",                    friends.list_friends),
    ("PUT",    "/friends/{id}",               friends.set_hide_lifts),
    ("DELETE", "/friends/{id}",               friends.remove_friend),
    ("GET",    "/friend-requests",            friends.list_requests),
    ("POST",   "/friend-requests",            friends.send_request),
    ("PUT",    "/friend-requests/{id}",       friends.answer_request),
    ("DELETE", "/friend-requests/{id}",       friends.cancel_request),
    ("GET",    "/athletes/{id}",              friends.get_friend),
    ("GET",    "/athletes/{id}/lifts",        friends.list_friend_lifts),
    ("GET",    "/athletes/{id}/lifts/{lift_id}", friends.get_friend_lift),
    ("GET",    "/athletes/{id}/programs",     friends.list_friend_programs),
    ("GET",    "/athletes/{id}/programs/{program_id}", friends.get_friend_program),
    ("POST",   "/athletes/{id}/programs/{program_id}/copy", friends.copy_friend_program),
    ("GET",    "/blocks",                     friends.list_blocks),
    ("POST",   "/blocks",                     friends.block_by_username),
    ("PUT",    "/blocks/{id}",                friends.block_athlete),
    ("DELETE", "/blocks/{id}",                friends.unblock_athlete),
]
ROUTES = [(method, _compile(path), fn, path) for method, path, fn in _ROUTES]
