/*
 * Conditional display/required rules for custom fields.
 *
 * The server (helpdesk/field_rules.py, applied in helpdesk/forms.py) is the
 * authority: it re-evaluates every rule on submit, hides errors for hidden
 * fields and enforces conditional required validation. This script only
 * mirrors the rules in the browser so submitters do not have to discover
 * the conditions via validation errors.
 *
 * Per-field widget attributes, populated by CustomFieldMixin:
 *   data-custom-field="<field name>"
 *   data-display-rules='{"*": [...predicates], "<queue id>": [...]}'
 *   data-required-rules='{"*": [...predicates], "<queue id>": [...]}'
 * Predicate source fields use the same ids as the form, e.g. id_queue,
 * id_priority or id_custom_<name>.
 */
(function () {
  "use strict";

  function parseRules(widget, attr) {
    var raw = widget.getAttribute(attr);
    if (!raw) {
      return null;
    }
    try {
      return JSON.parse(raw);
    } catch (e) {
      return null;
    }
  }

  function pickQueueRules(config, queueId) {
    if (!config) {
      return null;
    }
    if (queueId && Object.prototype.hasOwnProperty.call(config, String(queueId))) {
      return config[String(queueId)];
    }
    return config["*"] || null;
  }

  function fieldValue(form, elementId) {
    var el = form.elements.namedItem(elementId);
    if (!el) {
      return "";
    }
    // A single <select> also exposes a numeric `length` (its options), so it
    // must be handled before the radio/checkbox collection branch below.
    if (el.type === "select-one") {
      return el.value || "";
    }
    // Radio buttons and multi-checkbox come back as a collection.
    if (typeof el.length === "number" && el.type !== "select-multiple") {
      for (var i = 0; i < el.length; i++) {
        if (el[i].checked) {
          return el[i].value;
        }
      }
      return "";
    }
    if (el.type === "checkbox") {
      return el.checked ? "True" : "False";
    }
    if (el.type === "select-multiple") {
      return Array.prototype.filter
        .call(el.options, function (option) {
          return option.selected;
        })
        .map(function (option) {
          return option.value;
        })
        .join(",");
    }
    return el.value || "";
  }

  function evaluatePredicates(predicates, form) {
    if (!predicates || !predicates.length) {
      return true;
    }
    return predicates.every(function (predicate) {
      var sourceId = "id_" + predicate.field;
      var actual = fieldValue(form, sourceId);
      var operator = predicate.operator || "eq";
      var expected = predicate.value;
      if (operator === "empty") {
        return actual === "";
      }
      if (operator === "filled") {
        return actual !== "";
      }
      if (operator === "contains") {
        return actual.indexOf(String(expected)) !== -1;
      }
      if (operator === "in") {
        return (expected || []).map(String).indexOf(actual) !== -1;
      }
      if (operator === "not_in") {
        return (expected || []).map(String).indexOf(actual) === -1;
      }
      if (operator === "ne") {
        return actual !== String(expected);
      }
      return actual === String(expected);
    });
  }

  function rowOf(widget) {
    return widget.closest("fieldset, .mb-3, .form-check") || widget.parentNode;
  }

  function applyForm(form) {
    var widgets = form.querySelectorAll("[data-custom-field]");
    if (!widgets.length) {
      return;
    }
    var queueWidget = form.elements.namedItem("id_queue");
    var queueId = queueWidget ? queueWidget.value : "";

    Array.prototype.forEach.call(widgets, function (widget) {
      var name = widget.getAttribute("data-custom-field");
      var displayConfig = parseRules(widget, "data-display-rules");
      var requiredConfig = parseRules(widget, "data-required-rules");

      var visible = true;
      var required = widget.required;
      var displayPredicates = pickQueueRules(displayConfig, queueId);
      if (displayPredicates) {
        visible = evaluatePredicates(displayPredicates, form);
      }
      var requiredPredicates = pickQueueRules(requiredConfig, queueId);
      if (requiredPredicates) {
        required = visible && evaluatePredicates(requiredPredicates, form);
      }

      var row = rowOf(widget);
      row.hidden = !visible;
      if (visible && required) {
        widget.setAttribute("required", "required");
      } else {
        widget.removeAttribute("required");
      }

      var label = row.querySelector("label.form-label, legend");
      if (label) {
        label.classList.toggle("text-danger", visible && required);
      }

      // Edit page: when a field with a stored value becomes hidden, the
      // keep/clear confirmation row for it is shown. Clearing needs an
      // explicit tick; leaving it unticked keeps the old value.
      var clearBox = form.querySelector('[data-clear-for="custom_' + name + '"]');
      if (clearBox) {
        var clearRow = rowOf(clearBox);
        var currentValue = fieldValue(form, "id_custom_" + name);
        var hasValue = currentValue !== "" && currentValue !== "False";
        var showClearChoice = !visible && hasValue;
        clearRow.hidden = !showClearChoice;
        if (!showClearChoice) {
          clearBox.checked = false;
        }
      }
    });
  }

  function init() {
    var forms = document.querySelectorAll("form");
    Array.prototype.forEach.call(forms, function (form) {
      if (!form.querySelector("[data-custom-field]")) {
        return;
      }
      var scheduled = false;
      var schedule = function () {
        if (scheduled) {
          return;
        }
        scheduled = true;
        window.requestAnimationFrame(function () {
          scheduled = false;
          applyForm(form);
        });
      };
      form.addEventListener("change", schedule);
      form.addEventListener("input", schedule);
      applyForm(form);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
