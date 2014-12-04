""" API implementation for course-oriented interactions. """

from collections import OrderedDict
import logging
from lxml import etree
from StringIO import StringIO

from django.contrib.auth.models import Group, User
from django.core.exceptions import ObjectDoesNotExist
from django.http import Http404
from rest_framework import status
from rest_framework.response import Response
from lms.lib.comment_client.user import get_course_social_stats
from lms.lib.comment_client.thread import get_course_thread_stats
from lms.lib.comment_client.utils import CommentClientRequestError

from courseware.courses import get_course_about_section, get_course_info_section, course_image_url
from courseware.views import get_static_tab_contents
from django_comment_common.models import FORUM_ROLE_MODERATOR
from instructor.access import revoke_access, update_forum_role
from student.models import CourseEnrollment
from student.roles import CourseAccessRole, CourseInstructorRole, CourseStaffRole, CourseObserverRole, CourseAssistantRole, UserBasedRole
from server_api.models import CourseGroupRelationship, GroupProfile


from server_api.util.courseware_access import get_course, get_course_child, get_course_key, \
    course_exists, get_modulestore, get_course_descriptor, get_aggregate_exclusion_user_ids
from server_api.util.permissions import SecureAPIView, SecureListAPIView
from server_api.util.utils import generate_base_uri

from serializers import CourseSerializer


log = logging.getLogger(__name__)


def _get_content_children(content, content_type=None):
    """
    Parses the provided content object looking for children
    Matches on child content type (category) when specified
    """
    children = []
    if hasattr(content, 'children'):
        child_content = content.get_children()
        for child in child_content:
            if content_type:
                if getattr(child, 'category') == content_type:
                    children.append(child)
            else:
                children.append(child)
    return children


def _serialize_content(request, course_key, content_descriptor):
    """
    Loads the specified content object into the response dict
    This should probably evolve to use DRF serializers
    """
    protocol = 'http'
    if request.is_secure():
        protocol = protocol + 's'

    base_content_uri = '{}://{}/api/server/courses'.format(
        protocol,
        request.get_host()
    )

    data = {}

    if hasattr(content_descriptor, 'display_name'):
        data['name'] = content_descriptor.display_name

    if hasattr(content_descriptor, 'due'):
        data['due'] = content_descriptor.due

    data['start'] = getattr(content_descriptor, 'start', None)
    data['end'] = getattr(content_descriptor, 'end', None)

    data['category'] = content_descriptor.location.category

    # Some things we only do if the content object is a course
    if hasattr(content_descriptor, 'category') and content_descriptor.category == 'course':
        content_id = unicode(content_descriptor.id)
        content_uri = '{}/{}'.format(base_content_uri, content_id)
        data['number'] = content_descriptor.location.course
        data['org'] = content_descriptor.location.org

    # Other things we do only if the content object is not a course
    else:
        content_id = unicode(content_descriptor.location)
        # Need to use the CourseKey here, which will possibly result in a different (but valid)
        # URI due to the change in key formats during the "opaque keys" transition
        content_uri = '{}/{}/content/{}'.format(base_content_uri, unicode(course_key), content_id)

    data['id'] = unicode(content_id)
    data['uri'] = content_uri

    # Include any additional fields requested by the caller
    include_fields = request.QUERY_PARAMS.get('include_fields', None)
    if include_fields:
        include_fields = include_fields.split(',')
        for field in include_fields:
            data[field] = getattr(content_descriptor, field, None)

    return data


def _serialize_content_children(request, course_key, children):
    """
    Loads the specified content child data into the response dict
    This should probably evolve to use DRF serializers
    """
    data = []
    if children:
        for child in children:
            child_data = _serialize_content(
                request,
                course_key,
                child
            )
            data.append(child_data)
    return data


def _serialize_content_with_children(request, course_key, descriptor, depth):  # pylint: disable=C0103
    """
    Serializes course content and then dives into the content tree,
    serializing each child module until specified depth limit is hit
    """
    data = _serialize_content(
        request,
        course_key,
        descriptor
    )
    if depth > 0:
        data['children'] = []
        for child in descriptor.get_children():
            data['children'].append(_serialize_content_with_children(
                request,
                course_key,
                child,
                depth - 1
            ))
    return data


def _inner_content(tag):
    """
    Helper method
    """
    inner_content = None
    if tag is not None:
        inner_content = tag.text if tag.text else u''
        inner_content += u''.join(etree.tostring(e) for e in tag)
        inner_content += tag.tail if tag.tail else u''

    return inner_content


def _parse_overview_html(html):
    """
    Helper method to break up the course about HTML into components
    Overview content is stored in MongoDB (aka, the module store) with the following naming convention

            {
                "_id.org":"i4x",
                "_id.course":<course_num>,
                "_id.category":"about",
                "_id.name":"overview"
            }
    """

    parser = etree.HTMLParser()
    tree = etree.parse(StringIO(html), parser)

    sections = tree.findall('/body/section')

    result = []
    for section in sections:
        section_class = section.get('class')
        if section_class:
            section_data = OrderedDict()
            section_data['class'] = section_class

            section_data['attributes'] = {}
            for attribute_key in section.keys():
                # don't return the class attribute as we are already using the class attribute
                # as a key name to the result set, so we don't want to end up duplicating it
                if attribute_key != 'class':
                    section_data['attributes'][attribute_key] = section.get(attribute_key)

            articles = section.findall('article')
            if articles:
                section_data['articles'] = []
                for article in articles:
                    article_class = article.get('class')
                    if article_class:
                        article_data = OrderedDict()
                        article_data['class'] = article_class

                        if article_class == "teacher":

                            name_element = article.find('h3')
                            if name_element is not None:
                                article_data['name'] = name_element.text

                            image_element = article.find("./div[@class='teacher-image']/img")
                            if image_element is not None:
                                article_data['image_src'] = image_element.get('src')

                            bios = article.findall('p')
                            bio_html = ''
                            for bio in bios:
                                bio_html += etree.tostring(bio)

                            if bio_html:
                                article_data['bio'] = bio_html
                        else:
                            article_data['body'] = _inner_content(article)

                        section_data['articles'].append(article_data)
            else:
                section_data['body'] = _inner_content(section)

            result.append(section_data)

    return result


def _parse_updates_html(html):
    """
    Helper method to extract updates contained within the course info HTML into components
    Updates content is stored in MongoDB (aka, the module store) with the following naming convention

            {
                "_id.org":"i4x",
                "_id.course":<course_num>,
                "_id.category":"course_info",
                "_id.name":"updates"
            }
    """

    parser = etree.HTMLParser()
    tree = etree.parse(StringIO(html), parser)

    # get all of the individual postings
    postings = tree.findall('/body/section/article')

    # be backwards compatible
    if not postings:
        postings = tree.findall('/body/ol/li')

    result = []
    for posting in postings:
        posting_data = {}
        posting_date_element = posting.find('h2')
        if posting_date_element is not None:
            posting_data['date'] = posting_date_element.text

        content = u''
        for current_element in posting:
            # note, we can't delete or skip over the date element in
            # the HTML tree because there might be some tailing content
            if current_element != posting_date_element:
                content += etree.tostring(current_element)
            else:
                content += current_element.tail if current_element.tail else u''

        posting_data['content'] = content.strip()
        result.append(posting_data)

    return result


def _manage_role(course_descriptor, user, role, action):
    """
    Helper method for managing course/forum roles
    """
    supported_roles = ('instructor', 'staff', 'observer', 'assistant')
    forum_moderator_roles = ('instructor', 'staff', 'assistant')
    if role not in supported_roles:
        raise ValueError
    if action is 'allow':
        existing_role = CourseAccessRole.objects.filter(user=user, role=role, course_id=course_descriptor.id, org=course_descriptor.org)
        if not existing_role:
            new_role = CourseAccessRole(user=user, role=role, course_id=course_descriptor.id, org=course_descriptor.org)
            new_role.save()
        if role in forum_moderator_roles:
            update_forum_role(course_descriptor.id, user, FORUM_ROLE_MODERATOR, 'allow')
    elif action is 'revoke':
        revoke_access(course_descriptor, user, role)
        if role in forum_moderator_roles:
            # There's a possibilty that the user may play more than one role in a course
            # And that more than one of these roles allow for forum moderation
            # So we need to confirm the removed role was the only role for this user for this course
            # Before we can safely remove the corresponding forum moderator role
            user_instructor_courses = UserBasedRole(user, CourseInstructorRole.ROLE).courses_with_role()
            user_staff_courses = UserBasedRole(user, CourseStaffRole.ROLE).courses_with_role()
            user_assistant_courses = UserBasedRole(user, CourseAssistantRole.ROLE).courses_with_role()
            queryset = user_instructor_courses | user_staff_courses | user_assistant_courses
            queryset = queryset.filter(course_id=course_descriptor.id)
            if len(queryset) == 0:
                update_forum_role(course_descriptor.id, user, FORUM_ROLE_MODERATOR, 'revoke')


def _get_course_data(request, course_key, course_descriptor, depth=0):
    """
    creates a dict of course attributes
    """

    if depth > 0:
        data = _serialize_content_with_children(
            request,
            course_key,
            course_descriptor,  # Primer for recursive function
            depth
        )
        data['content'] = data['children']
        data.pop('children')
    else:
        data = _serialize_content(
            request,
            course_key,
            course_descriptor
        )
    base_uri_without_qs = generate_base_uri(request, True)
    if unicode(course_descriptor.id) not in base_uri_without_qs:
        base_uri_without_qs = '{}/{}'.format(base_uri_without_qs, unicode(course_descriptor.id))
    image_url = ''
    if hasattr(course_descriptor, 'course_image') and course_descriptor.course_image:
        image_url = course_image_url(course_descriptor)
    data['course_image_url'] = image_url
    data['resources'] = []
    resource_uri = '{}/content/'.format(base_uri_without_qs)
    data['resources'].append({'uri': resource_uri})
    resource_uri = '{}/overview/'.format(base_uri_without_qs)
    data['resources'].append({'uri': resource_uri})
    resource_uri = '{}/updates/'.format(base_uri_without_qs)
    data['resources'].append({'uri': resource_uri})
    resource_uri = '{}/static_tabs/'.format(base_uri_without_qs)
    data['resources'].append({'uri': resource_uri})
    return data


class CourseContentList(SecureAPIView):
    """
    **Use Case**

        CourseContentList gets a collection of content for a given
        course. You can use the **uri** value in
        the response to get details for that content entity.

        CourseContentList has an optional type parameter that allows you to
        filter the response by content type. The value of the type parameter
        matches the category value in the response. Valid values for the type
        parameter are:

        * chapter
        * sequential
        * vertical
        * html
        * problem
        * discussion
        * video
        * [CONFIRM]

    **Example requests**:

        GET /api/courses/{course_id}/content

        GET /api/courses/{course_id}/content?type=video

        GET /api/courses/{course_id}/content/{content_id}/children

    **Response Values**

        * category: The type of content.

        * due: The due date.

        * uri: The URI to use to get details of the content entity.

        * id: The unique identifier for the content entity.

        * name: The name of the course.
    """

    def get(self, request, course_id, content_id=None):
        """
        GET /api/courses/{course_id}/content
        """
        course_descriptor, course_key, course_content = get_course(request, request.user, course_id)  # pylint: disable=W0612
        if not course_descriptor:
            return Response({}, status=status.HTTP_404_NOT_FOUND)
        if content_id is None:
            content_id = course_id
        response_data = []
        content_type = request.QUERY_PARAMS.get('type', None)
        if course_id != content_id:
            content_descriptor, content_key, content = get_course_child(request, request.user, course_key, content_id, load_content=True)  # pylint: disable=W0612
        else:
            content = course_descriptor
        if content:
            children = _get_content_children(content, content_type)
            response_data = _serialize_content_children(
                request,
                course_key,
                children
            )
            status_code = status.HTTP_200_OK
        else:
            status_code = status.HTTP_404_NOT_FOUND
        return Response(response_data, status=status_code)


class CourseContentDetail(SecureAPIView):
    """
    **Use Case**

        CourseContentDetail returns a JSON collection for a specified
        CourseContent entity. If the specified CourseContent is the Course, the
        course representation is returned. You can use the uri values in the
        children collection in the JSON response to get details for that content
        entity.

        CourseContentDetail has an optional type parameter that allows you to
        filter the response by content type. The value of the type parameter
        matches the category value in the response. Valid values for the type
        parameter are:

        * chapter
        * sequential
        * vertical
        * html
        * problem
        * discussion
        * video
        * [CONFIRM]

    **Example Request**

          GET /api/courses/{course_id}/content/{content_id}

    **Response Values**

        * category: The type of content.

        * name: The name of the content entity.

        * due:  The due date.

        * uri: The URI of the content entity.

        * id: The unique identifier for the course.

        * children: Content entities that this conent entity contains.

        * resources: A list of URIs to available users and groups:
          * Related Users  /api/courses/{course_id}/content/{content_id}/users
          * Related Groups /api/courses/{course_id}/content/{content_id}/groups
    """

    def get(self, request, course_id, content_id):
        """
        GET /api/courses/{course_id}/content/{content_id}
        """
        content, course_key, course_content = get_course(request, request.user, course_id)  # pylint: disable=W0612
        response_data = {}
        base_uri = generate_base_uri(request)
        response_data['uri'] = base_uri
        if course_id != content_id:
            element_name = 'children'
            content_descriptor, content_key, content = get_course_child(request, request.user, course_key, content_id, load_content=True)  # pylint: disable=W0612
        else:
            element_name = 'content'
            protocol = 'http'
            if request.is_secure():
                protocol = protocol + 's'
            response_data['uri'] = '{}://{}/api/server/courses/{}'.format(
                protocol,
                request.get_host(),
                unicode(course_key)
            )
        if not content:
            return Response(response_data, status=status.HTTP_404_NOT_FOUND)
        response_data = _serialize_content(
            request,
            course_id,
            content
        )
        content_type = request.QUERY_PARAMS.get('type', None)
        children = _get_content_children(content, content_type)
        response_data[element_name] = _serialize_content_children(
            request,
            course_id,
            children
        )
        base_uri_without_qs = generate_base_uri(request, True)
        resource_uri = '{}/groups'.format(base_uri_without_qs)
        response_data['resources'] = []
        response_data['resources'].append({'uri': resource_uri})
        resource_uri = '{}/users'.format(base_uri_without_qs)
        response_data['resources'].append({'uri': resource_uri})
        return Response(response_data, status=status.HTTP_200_OK)


class CoursesList(SecureListAPIView):
    """
    **Use Case**

        CoursesList returns paginated list of courses in the edX Platform. You can
        use the uri value in the response to get details of the course. course list can be
        filtered by course_id

    **Example Request**

          GET /api/courses
          GET /api/courses/?course_id={course_id1},{course_id2}

    **Response Values**

        * category: The type of content. In this case, the value is always "course".

        * name: The name of the course.

        * uri: The URI to use to get details of the course.

        * number: The course number.

        * due:  The due date. For courses, the value is always null.

        * org: The organization specified for the course.

        * id: The unique identifier for the course.
    """
    serializer_class = CourseSerializer

    def get_queryset(self):
        course_ids = self.request.QUERY_PARAMS.get('course_id', None)
        depth = self.request.QUERY_PARAMS.get('depth', 0)
        course_descriptors = []
        if course_ids:
            course_ids = course_ids.split(',')
            for course_id in course_ids:
                course_key = get_course_key(course_id)
                course_descriptor = get_course_descriptor(course_key, 0)
                course_descriptors.append(course_descriptor)
        else:
            course_descriptors = get_modulestore().get_courses()

        results = [_get_course_data(self.request, descriptor.id, descriptor, depth)
                   for descriptor in course_descriptors]
        return results


class CoursesDetail(SecureAPIView):
    """
    **Use Case**

        CoursesDetail returns details for a course. You can use the uri values
        in the resources collection in the response to get more course
        information for:

        * Users (/api/courses/{course_id}/users/)
        * Groups (/api/courses/{course_id}/groups/)
        * Course Overview (/api/courses/{course_id}/overview/)
        * Course Updates (/api/courses/{course_id}/updates/)
        * Course Pages (/api/courses/{course_id}/static_tabs/)

        CoursesDetail has an optional **depth** parameter that allows you to
        get course content children to the specified tree level.

    **Example requests**:

        GET /api/courses/{course_id}

        GET /api/courses/{course_id}?depth=2

    **Response Values**

        * category: The type of content.

        * name: The name of the course.

        * uri: The URI to use to get details of the course.

        * number: The course number.

        * content: When the depth parameter is used, a collection of child
          course content entities, such as chapters, sequentials, and
          components.

        * due:  The due date. For courses, the value is always null.

        * org: The organization specified for the course.

        * id: The unique identifier for the course.

        * resources: A collection of URIs to use to get more information about
          the course.
    """

    def get(self, request, course_id):
        """
        GET /api/courses/{course_id}
        """
        depth = request.QUERY_PARAMS.get('depth', 0)
        depth_int = int(depth)
        # get_course_by_id raises an Http404 if the requested course is invalid
        # Rather than catching it, we just let it bubble up
        course_descriptor, course_key, course_content = get_course(request, request.user, course_id, depth=depth_int)  # pylint: disable=W0612
        if not course_descriptor:
            return Response({}, status=status.HTTP_404_NOT_FOUND)
        response_data = _get_course_data(request, course_key, course_descriptor, depth_int)
        return Response(response_data, status=status.HTTP_200_OK)


class CoursesGroupsList(SecureAPIView):
    """
    **Use Case**

        CoursesGroupsList returns a collection of course group relationship
        entities(?) for a specified course entity.

        CoursesGroupsList has an optional **type** parameter that allows you to
        filter the groups returned. Valid values for the type parameter are:

        * [CONFIRM]

    **Example Request**

        GET /api/courses/{course_id}/groups?type=workgroup

        POST /api/courses/{course_id}/groups

    **Response Values**


    ### The CoursesGroupsList view allows clients to retrieve a list of Groups for a given Course entity
    - URI: ```/api/courses/{course_id}/groups/```
    - GET: Returns a JSON representation (array) of the set of CourseGroupRelationship entities
        * type: Set filtering parameter
    - POST: Creates a new relationship between the provided Course and Group
        * group_id: __required__, The identifier for the Group with which we're establishing a relationship
    - POST Example:

            {
                "group_id" : 12345,
            }
    ### Use Cases/Notes:
    * Example: Display all of the courses for a particular academic series/program
    * If a relationship already exists between a Course and a particular group, the system returns 409 Conflict
    * The 'type' parameter filters groups by their 'group_type' field ('workgroup', 'series', etc.)
    """

    def post(self, request, course_id):
        """
        POST /api/courses/{course_id}/groups
        """
        response_data = {}
        group_id = request.DATA['group_id']
        base_uri = generate_base_uri(request)
        if not course_exists(course_id):
            return Response({}, status=status.HTTP_404_NOT_FOUND)
        course_key = get_course_key(course_id)
        try:
            existing_group = Group.objects.get(id=group_id)
        except ObjectDoesNotExist:
            existing_group = None
        if existing_group:
            try:
                existing_relationship = CourseGroupRelationship.objects.get(course_id=course_key, group=existing_group)
            except ObjectDoesNotExist:
                existing_relationship = None
            if existing_relationship is None:
                CourseGroupRelationship.objects.create(course_id=course_key, group=existing_group)
                response_data['course_id'] = unicode(course_key)
                response_data['group_id'] = str(existing_group.id)
                response_data['uri'] = '{}/{}'.format(base_uri, existing_group.id)
                response_status = status.HTTP_201_CREATED
            else:
                response_data['message'] = "Relationship already exists."
                response_status = status.HTTP_409_CONFLICT
        else:
            response_status = status.HTTP_404_NOT_FOUND
        return Response(response_data, status=response_status)

    def get(self, request, course_id):
        """
        GET /api/courses/{course_id}/groups?type=workgroup
        """
        if not course_exists(course_id):
            return Response({}, status=status.HTTP_404_NOT_FOUND)
        group_type = request.QUERY_PARAMS.get('type', None)
        course_key = get_course_key(course_id)
        course_groups = CourseGroupRelationship.objects.filter(course_id=course_key)
        if group_type:
            course_groups = course_groups.filter(group__groupprofile__group_type=group_type)
        response_data = []
        for course_group in course_groups:
            group_profile = GroupProfile.objects.get(group_id=course_group.group_id)
            group_data = {'id': course_group.group_id, 'name': group_profile.name}
            response_data.append(group_data)
        response_status = status.HTTP_200_OK
        return Response(response_data, status=response_status)


class CoursesGroupsDetail(SecureAPIView):
    """
    ### The CoursesGroupsDetail view allows clients to interact with a specific CourseGroupRelationship entity
    - URI: ```/api/courses/{course_id}/group/{group_id}```
    - GET: Returns a JSON representation of the specified CourseGroupRelationship entity
        * type: Set filtering parameter
    - DELETE: Removes an existing CourseGroupRelationship from the system
    ### Use Cases/Notes:
    * Use this operation to confirm the existence of a specific Course-Group entity relationship
    """

    def get(self, request, course_id, group_id):
        """
        GET /api/courses/{course_id}/groups/{group_id}
        """
        if not course_exists(course_id):
            return Response({}, status=status.HTTP_404_NOT_FOUND)
        try:
            existing_group = Group.objects.get(id=group_id)
        except ObjectDoesNotExist:
            return Response({}, status=status.HTTP_404_NOT_FOUND)
        try:
            course_key = get_course_key(course_id)
            CourseGroupRelationship.objects.get(course_id=course_key, group=existing_group)
        except ObjectDoesNotExist:
            return Response({}, status=status.HTTP_404_NOT_FOUND)
        response_data = {}
        base_uri = generate_base_uri(request)
        response_data['uri'] = base_uri
        response_data['course_id'] = course_id
        response_data['group_id'] = group_id
        return Response(response_data, status=status.HTTP_200_OK)

    def delete(self, request, course_id, group_id):
        """
        DELETE /api/courses/{course_id}/groups/{group_id}
        """
        if not course_exists(course_id):
            return Response({}, status=status.HTTP_204_NO_CONTENT)
        try:
            existing_group = Group.objects.get(id=group_id)
            course_key = get_course_key(course_id)
            CourseGroupRelationship.objects.get(course_id=course_key, group=existing_group).delete()
        except ObjectDoesNotExist:
            pass
        response_data = {}
        response_data['uri'] = generate_base_uri(request)
        return Response(response_data, status=status.HTTP_204_NO_CONTENT)


class CoursesOverview(SecureAPIView):
    """
    **Use Case**

        CoursesOverview returns an HTML representation of the overview for the
        specified course. CoursesOverview has an optional parse parameter that
        when true breaks the response into a collection named sections. By
        default, parse is false.

    **Example Request**

          GET /api/courses/{course_id}/overview

          GET /api/courses/{course_id}/overview?parse=true

    **Response Values**

        * overview_html: The HTML representation of the course overview.
          Sections of the overview are indicated by an HTML section element.

        * sections: When parse=true, a collection of JSON objects representing
          parts of the course overview.

    """

    def get(self, request, course_id):
        """
        GET /api/courses/{course_id}/overview
        """
        response_data = OrderedDict()
        course_descriptor, course_key, course_content = get_course(request, request.user, course_id)  # pylint: disable=W0612
        if not course_descriptor:
            return Response({}, status=status.HTTP_404_NOT_FOUND)
        existing_content = get_course_about_section(course_descriptor, 'overview')
        if not existing_content or not len(existing_content):
            return Response({}, status=status.HTTP_404_NOT_FOUND)
        if request.GET.get('parse') and request.GET.get('parse') in ['True', 'true']:
            response_data['sections'] = _parse_overview_html(existing_content)
        else:
            response_data['overview_html'] = existing_content
        image_url = ''
        if hasattr(course_descriptor, 'course_image') and course_descriptor.course_image:
            image_url = course_image_url(course_descriptor)
        response_data['course_image_url'] = image_url
        response_data['course_video'] = get_course_about_section(course_descriptor, 'video')
        return Response(response_data, status=status.HTTP_200_OK)


class CoursesUpdates(SecureAPIView):
    """
    **Use Case**

        CoursesUpdates returns an HTML representation of the overview for the
        specified course. CoursesUpdates has an optional parse parameter that
        when true breaks the response into a collection named postings. By
        default, parse is false.

    **Example Requests**

          GET /api/courses/{course_id}/updates

          GET /api/courses/{course_id}/updates?parse=true

    **Response Values**

        * content: The HTML representation of the course overview.
          Sections of the overview are indicated by an HTML section element.

        * postings: When parse=true, a collection of JSON objects representing
          parts of the course overview. Each element in postings contains a date
          and content key.
    """

    def get(self, request, course_id):
        """
        GET /api/courses/{course_id}/updates
        """
        course_descriptor, course_key, course_content = get_course(request, request.user, course_id)  # pylint: disable=W0612
        if not course_descriptor:
            return Response({}, status=status.HTTP_404_NOT_FOUND)
        response_data = OrderedDict()
        content = get_course_info_section(request, course_descriptor, 'updates')
        if not content:
            return Response({}, status=status.HTTP_404_NOT_FOUND)
        if request.GET.get('parse') and request.GET.get('parse') in ['True', 'true']:
            response_data['postings'] = _parse_updates_html(content)
        else:
            response_data['content'] = content
        return Response(response_data)


class CoursesStaticTabsList(SecureAPIView):
    """
    **Use Case**

        CoursesStaticTabsList returns a collection of custom pages in the
        course. CoursesStaticTabsList has an optional detail parameter that when
        true includes the custom page content in the response.

    **Example Requests**

          GET /api/courses/{course_id}/static_tabs

          GET /api/courses/{course_id}/static_tabs?detail=true

    **Response Values**

        * tabs: The collection of custom pages in the course. Each object in the
          collection conains the following keys:

          * id: The ID of the custom page.

          * name: The Display Name of the custom page.

          * detail: When detail=true, the content of the custom page as HTML.
    """

    def get(self, request, course_id):
        """
        GET /api/courses/{course_id}/static_tabs
        """
        course_descriptor, course_key, course_content = get_course(request, request.user, course_id)  # pylint: disable=W0612
        if not course_descriptor:
            return Response({}, status=status.HTTP_404_NOT_FOUND)
        response_data = OrderedDict()
        tabs = []
        for tab in course_descriptor.tabs:
            if tab.type == 'static_tab':
                tab_data = OrderedDict()
                tab_data['id'] = tab.url_slug
                tab_data['name'] = tab.name
                if request.GET.get('detail') and request.GET.get('detail') in ['True', 'true']:
                    tab_data['content'] = get_static_tab_contents(
                        request,
                        course_descriptor,
                        tab,
                        wrap_xmodule_display=False
                    )
                tabs.append(tab_data)
        response_data['tabs'] = tabs
        return Response(response_data)


class CoursesStaticTabsDetail(SecureAPIView):
    """
    **Use Case**

        CoursesStaticTabsDetail returns a collection of custom pages in the
        course, including the page content.

    **Example Requests**

          GET /api/courses/{course_id}/static_tabs/{tab_id}

    **Response Values**

        * tabs: The collection of custom pages in the course. Each object in the
          collection conains the following keys:

          * id: The ID of the custom page.

          * name: The Display Name of the custom page.

          * detail: The content of the custom page as HTML.
    """

    def get(self, request, course_id, tab_id):
        """
        GET /api/courses/{course_id}/static_tabs/{tab_id}
        """
        course_descriptor, course_key, course_content = get_course(request, request.user, course_id)  # pylint: disable=W0612
        if not course_descriptor:
            return Response({}, status=status.HTTP_404_NOT_FOUND)
        response_data = OrderedDict()
        for tab in course_descriptor.tabs:
            if tab.type == 'static_tab' and tab.url_slug == tab_id:
                response_data['id'] = tab.url_slug
                response_data['name'] = tab.name
                response_data['content'] = get_static_tab_contents(
                    request,
                    course_descriptor,
                    tab,
                    wrap_xmodule_display=False
                )
        if not response_data:
            return Response({}, status=status.HTTP_404_NOT_FOUND)
        return Response(response_data, status=status.HTTP_200_OK)


class CoursesMetrics(SecureAPIView):
    """
    ### The CoursesMetrics view allows clients to retrieve a list of Metrics for the specified Course
    - URI: ```/api/courses/{course_id}/metrics/?organization={organization_id}```
    - GET: Returns a JSON representation (array) of the set of course metrics
    - metrics can be filtered by organization by adding organization parameter to GET request
    ### Use Cases/Notes:
    * Example: Display number of users enrolled in a given course
    """

    def get(self, request, course_id):  # pylint: disable=W0613
        """
        GET /api/courses/{course_id}/metrics/
        """
        if not course_exists(course_id):
            return Response({}, status=status.HTTP_404_NOT_FOUND)
        course_descriptor, course_key, __ = get_course(request, request.user, course_id)
        slash_course_id = get_course_key(course_id, slashseparated=True)
        exclude_users = get_aggregate_exclusion_user_ids(course_key)
        users_enrolled_qs = CourseEnrollment.users_enrolled_in(course_key).exclude(id__in=exclude_users)
        data = {
            'users_enrolled': users_enrolled_qs.count(),
            'grade_cutoffs': course_descriptor.grading_policy['GRADE_CUTOFFS']
        }

        thread_stats = {}
        try:
            thread_stats = get_course_thread_stats(slash_course_id)
        except CommentClientRequestError, err:
            data = {
                "err_msg": str(err)
            }
            return Response(data, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        data.update(thread_stats)
        return Response(data, status=status.HTTP_200_OK)


class CoursesMetricsSocial(SecureListAPIView):
    """
    ### The CoursesMetricsSocial view allows clients to query about the activity of all users in the
    forums
    - URI: ```/api/users/{course_id}/metrics/social/?organization={org_id}```
    - GET: Returns a list of social metrics for users in the specified course. Results can be filtered by organization
    """

    def get(self, request, course_id):  # pylint: disable=W0613,W0221

        try:
            course_key = get_course_key(course_id)

            # the forum service expects the legacy slash separated string format
            slash_course_id = get_course_key(course_id, slashseparated=True)
            data = get_course_social_stats(slash_course_id)

            # remove any excluded users from the aggregate
            exclude_users = get_aggregate_exclusion_user_ids(course_key)

            for user_id in exclude_users:
                if str(user_id) in data:
                    del data[str(user_id)]
            enrollment_qs = CourseEnrollment.users_enrolled_in(course_key).filter(is_active=True)\
                .exclude(id__in=exclude_users)
            actual_data = {}

            actual_users = enrollment_qs.values_list('id', flat=True)
            for user_id in actual_users:
                if str(user_id) in data:
                    actual_data.update({str(user_id): data[str(user_id)]})

            data = actual_data
            total_enrollments = enrollment_qs.count()
            data = {'total_enrollments': total_enrollments, 'users': data}
            http_status = status.HTTP_200_OK
        except CommentClientRequestError, err:
            data = {
                "err_msg": str(err)
            }
            http_status = status.HTTP_500_INTERNAL_SERVER_ERROR

        return Response(data, http_status)


class CoursesRolesList(SecureAPIView):
    """
    ### The CoursesRolesList view allows clients to interact with the Course's roleset
    - URI: ```/api/courses/{course_id}/roles```
    - GET: Returns a JSON representation of the specified Course roleset

    ### Use Cases/Notes:
    * Use the CoursesRolesList view to manage a User's TA status
    * Use GET to retrieve the set of roles configured for a particular course
    """

    def get(self, request, course_id):  # pylint: disable=W0613
        """
        GET /api/courses/{course_id}/roles/
        """
        course_id = self.kwargs['course_id']
        if not course_exists(course_id):
            raise Http404

        response_data = []
        course_key = get_course_key(course_id)
        instructors = CourseInstructorRole(course_key).users_with_role()
        for instructor in instructors:
            response_data.append({'id': instructor.id, 'role': 'instructor'})

        staff = CourseStaffRole(course_key).users_with_role()
        for admin in staff:
            response_data.append({'id': admin.id, 'role': 'staff'})

        observers = CourseObserverRole(course_key).users_with_role()
        for observer in observers:
            response_data.append({'id': observer.id, 'role': 'observer'})

        assistants = CourseAssistantRole(course_key).users_with_role()
        for assistant in assistants:
            response_data.append({'id': assistant.id, 'role': 'assistant'})

        user_id = self.request.QUERY_PARAMS.get('user_id', None)
        if user_id:
            response_data = list([item for item in response_data if int(item['id']) == int(user_id)])

        role = self.request.QUERY_PARAMS.get('role', None)
        if role:
            response_data = list([item for item in response_data if item['role'] == role])

        return Response(response_data, status=status.HTTP_200_OK)

    def post(self, request, course_id):
        """
        POST /api/courses/{course_id}/roles/
        """
        course_id = self.kwargs['course_id']
        course_descriptor, course_key, course_content = get_course(self.request, self.request.user, course_id)  # pylint: disable=W0612
        if not course_descriptor:
            raise Http404

        user_id = request.DATA.get('user_id', None)
        try:
            user = User.objects.get(id=user_id)
        except ObjectDoesNotExist:
            return Response({}, status=status.HTTP_400_BAD_REQUEST)

        role = request.DATA.get('role', None)
        try:
            _manage_role(course_descriptor, user, role, 'allow')
        except ValueError:
            return Response({}, status=status.HTTP_400_BAD_REQUEST)
        return Response(request.DATA, status=status.HTTP_201_CREATED)


class CoursesRolesUsersDetail(SecureAPIView):
    """
    ### The CoursesUsersRolesDetail view allows clients to interact with a specific Course Role
    - URI: ```/api/courses/{course_id}/roles/{role}/users/{user_id}```
    - DELETE: Removes an existing Course Role specification
    ### Use Cases/Notes:
    * Use the DELETE operation to revoke a particular role for the specified user
    """
    def delete(self, request, course_id, role, user_id):  # pylint: disable=unused-argument
        """
        DELETE /api/courses/{course_id}/roles/{role}/users/{user_id}
        """
        course_descriptor, course_key, course_content = get_course(self.request, self.request.user, course_id)
        if not course_descriptor:
            return Response({}, status=status.HTTP_404_NOT_FOUND)

        try:
            user = User.objects.get(id=user_id)
        except ObjectDoesNotExist:
            return Response({}, status=status.HTTP_404_NOT_FOUND)

        try:
            _manage_role(course_descriptor, user, role, 'revoke')
        except ValueError:
            return Response({}, status=status.HTTP_404_NOT_FOUND)

        return Response({}, status=status.HTTP_204_NO_CONTENT)
