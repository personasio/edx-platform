define([
    'annotator', 'js/edxnotes/views/notes_factory', 'js/common_helpers/ajax_helpers',
    'js/spec/edxnotes/custom_matchers'
], function(Annotator, NotesFactory, AjaxHelpers, customMatchers) {
    'use strict';
    describe('EdxNotes NotesFactory', function() {
        var wrapper;

        beforeEach(function() {
            customMatchers(this);
            loadFixtures('js/fixtures/edxnotes/edxnotes_wrapper.html');
            this.wrapper = document.getElementById('edx-notes-wrapper-123');
        });

        afterEach(function () {
            _.invoke(Annotator._instances, 'destroy');
        });

        it('can initialize annotator correctly', function() {
            var requests = AjaxHelpers.requests(this),
                options = {
                    user: 'a user',
                    usage_id : 'an usage',
                    course_id: 'a course'
                },
                annotator = NotesFactory.factory(this.wrapper, {
                    endpoint: 'test_endpoint',
                    user: 'a user',
                    usageId : 'an usage',
                    courseId: 'a course',
                    token: 'test_token'
                }),
                request = requests[0];

            expect(requests).toHaveLength(1);
            expect(request.requestHeaders['x-annotator-auth-token']).toBe('test_token');
            expect(annotator.options.store.prefix).toBe('test_endpoint');
            expect(annotator.options.store.annotationData).toEqual(options);
            expect(annotator.options.store.loadFromSearch).toEqual(options);
        });
    });
});
