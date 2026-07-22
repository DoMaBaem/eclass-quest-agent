"""한성 e-Class 화면에서 사용하는 선택자를 한 곳에 모은다.

실제 화면이 변경되면 Adapter 본문이 아니라 이 파일만 우선 수정한다.
"""

from __future__ import annotations


class HansungSelectors:
    """화면 목적별 후보 CSS 선택자 모음.

    첫 후보가 없으면 다음 후보를 시도한다. LMS 개편 시 Adapter 로직을 건드리기 전에 이 목록을
    실제 DOM에 맞춰 수정한다.
    """

    # 로그인 성공을 가장 확실히 보여 주는 로그아웃 링크/버튼 후보.
    LOGIN_SUCCESS = (
        "a:has-text('Log out')",
        "button:has-text('Log out')",
        "a:has-text('로그아웃')",
        "button:has-text('로그아웃')",
        "a[href*='/login/logout.php']",
    )
    # 로그인 언어와 무관하게 유지되는 실제 id/name을 우선한다.
    LOGIN_USERNAME = (
        "#input-username",
        "input[name='username']",
    )
    LOGIN_PASSWORD = (
        "#input-password",
        "input[name='password']",
    )
    LOGIN_SUBMIT = (
        "input[type='submit'][name='loginbutton']",
        "button[type='submit']",
    )
    # 메뉴 문구가 '나의 강좌' 또는 '수강 강좌'로 다르게 표시되는 경우를 모두 허용한다.
    MY_COURSES = (
        "a[href$='/local/ubion/user/']",
        "a:has-text('My Course')",
        "a:has-text('나의 강좌')",
        "a:has-text('수강 강좌')",
        "button:has-text('나의 강좌')",
        "button:has-text('수강 강좌')",
    )
    # name/id의 대소문자와 LMS별 명명 차이를 고려한 연도 select 후보.
    YEAR_SELECT = (
        "select[name*='year' i]",
        "select[id*='year' i]",
        "select[name*='s_year' i]",
    )
    SEMESTER_SELECT = (
        "select[name*='semester' i]",
        "select[id*='semester' i]",
        "select[name*='term' i]",
        "select[name*='hakgi' i]",
    )
    COURSE_LINKS = (
        ".my-course-lists a.coursefullname[href*='/course/view.php?id=']",
    )
    # 실제 강좌 페이지에서 확인한 Moodle 모듈 URL을 우선 사용한다. 문구는 언어 설정에 따라
    # 달라질 수 있으므로 보조 후보로만 둔다.
    ASSIGNMENT_LINKS = (
        "a[href*='/mod/assign/view.php?id=']",
        "a[href*='/mod/assign/index.php?id=']",
        "a:has-text('과제')",
        "a:has-text('Assignment')",
    )
    LECTURE_LINKS = (
        "a[href*='/mod/vod/view.php?id=']",
        "a[href*='/mod/vod/index.php?id=']",
        "a:has-text('VOD')",
    )
    ANNOUNCEMENT_LINKS = (
        "a[href*='/mod/ubboard/view.php?id=']",
        "a[href*='/mod/ubboard/article.php?id=']",
        "a:has-text('공지사항')",
        "a:has-text('Announcements')",
    )
    GRADE_LINKS = (
        "a[href*='/grade/report/user/index.php?id=']",
        "a.submenu-grade",
        "a:has-text('성적')",
        "a:has-text('Grades')",
    )
