var edx = edx || {};

(function($) {
    'use strict';

    edx.shoppingcart = edx.shoppingcart || {};
    edx.shoppingcart.showcart = {};

    /**
     * View for making shoppingcart
     * @constructor
     * @param {Object} params
     * @param {Object} params.el - The payment form element.
     */
    edx.shoppingcart.showcart.CartView = function(params) {
        /**
         * cart view that checks that all the cart items are valid (course enrollment is closed or not)
         * before the form submitted to the payment processor.
         * @param {Object} form - The form to modify.
         */

        /**
        * Check for all the cart items are still valid (courses enrollment are not closed)
        *
        * @returns {Object} The promise from the AJAX call to the server,
        *     which checks for cart items are valid or not and returns the boolean
        *     { is_course_enrollment_closed: <boolead> }
        */
        var isCourseEnrollmentAllowed = function() {
            return $.ajax({
                url: "/shoppingcart/is_course_enrollment_allowed/",
                type: "GET"
            });
        };

        var view = {
            /**
            * Initialize the view.
            *
            * @param {Object} params
            * @param {JQuery selector} params.el - The payment form element.
            * @returns {CartView}
            */
            initialize: function(params) {
                this.$el = params.el;
                _.bindAll(view,
                    'submit', 'responseFromServer',
                    'submitPaymentForm'
                );
                return this;
            },

            /**
            * Handle a click event on the "payment form submit" button.
            * This will contact the LMS server to check for all the
            * valid cart items (courses enrollment should not be closed at this point)
            * then send the user to the external payment processor or redirects to the
            * dashboard page
            *
            * @param {Object} event - The click event.
            */
            submit: function(event) {
                // Prevent form submission
                if (event) {
                    event.preventDefault();
                }

                this.$paymentForm = this.$el;
                isCourseEnrollmentAllowed()
                    .done(this.responseFromServer)
                return this;
            },

            /**
            * Send signed payment parameters to the external
            * payment processor if cart items are valid else redirect to
            * shoppingcart.
            *
            * @param {boolean} data.is_course_enrollment_closed
            */
            responseFromServer: function(data) {
                if (data.is_course_enrollment_closed == true) {
                    location.href = "/shoppingcart";
                }
                else {
                    this.submitPaymentForm(this.$paymentForm);
                }
            },

            /**
            * Submit the payment from to the external payment processor.
            *
            * @param {Object} form 
            */
            submitPaymentForm: function(form) {
                form.submit();
            }
        };

        view.initialize(params);
        return view;
    };

    $(document).ready(function() {
        // (click on the payment submit button).
        $('.cart-view form input[type="submit"]').click(function(event) {
            var container = $('.confirm-enrollment.cart-view form');
            var view = new edx.shoppingcart.showcart.CartView({
                el:container
            }).submit(event);
        });
    });
})(jQuery);