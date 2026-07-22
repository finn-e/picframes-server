import pytest
import json
from db import create_user, get_user_by_username, get_friends, add_friend_by_code
from db import load_playlist, load_playlists, create_playlist, is_playlist_accessible

def test_user_friend_code_generation(logged_in):
    # Retrieve newly created users
    user1 = get_user_by_username('admin')
    assert user1 is not None
    assert user1['friend_code'] is not None
    assert len(user1['friend_code']) == 9  # xxxx-xxxx format (8 chars + 1 hyphen)
    assert '-' in user1['friend_code']

def test_add_friend_endpoints(logged_in):
    # Create user 2
    uid2 = create_user('wife', 'wife')
    assert uid2 is not None
    user2 = get_user_by_username('wife')
    code2 = user2['friend_code']

    # Logged-in user (admin) adds user 2 as friend via API
    r = logged_in.post('/admin/friends/add', json={'code': code2})
    assert r.status_code == 200
    assert r.get_json()['ok'] is True
    assert r.get_json()['username'] == 'wife'

    # Check friends list for admin
    r_list = logged_in.get('/admin/friends')
    assert r_list.status_code == 200
    friends = r_list.get_json()['friends']
    assert len(friends) == 1
    assert friends[0]['username'] == 'wife'

def test_cannot_add_self(logged_in):
    user = get_user_by_username('admin')
    r = logged_in.post('/admin/friends/add', json={'code': user['friend_code']})
    assert r.status_code == 400
    assert 'cannot add yourself' in r.get_json()['error']

def test_playlist_sharing_and_access(logged_in, client):
    # admin creates a playlist
    pid = create_playlist('AdminPL', owner_id=1)
    assert pid is not None

    # create and login wife
    uid2 = create_user('wife2', 'wife2')
    user2 = get_user_by_username('wife2')
    
    # Establish friendship
    # admin adds wife2
    add_friend_by_code(1, user2['friend_code'])

    # Verify wife2 cannot access admin's playlist yet
    assert is_playlist_accessible(pid, uid2) is False

    # admin shares playlist with wife2
    r_share = logged_in.post(f'/admin/playlists/{pid}/share', json={'friend_id': uid2})
    assert r_share.status_code == 200
    assert r_share.get_json()['ok'] is True

    # Verify wife2 can now access admin's playlist
    assert is_playlist_accessible(pid, uid2) is True

    # admin unshares playlist
    r_unshare = logged_in.post(f'/admin/playlists/{pid}/unshare', json={'friend_id': uid2})
    assert r_unshare.status_code == 200
    assert r_unshare.get_json()['ok'] is True

    # Verify access is revoked
    assert is_playlist_accessible(pid, uid2) is False
