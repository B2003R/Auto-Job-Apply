globalThis.JOBRIGHT_STUB_GAP_CONTRACT = {
  "greenhouse": {
    "detect": {
      "selector": "[data-source=\"greenhouse\"]"
    },
    "required_input_left_empty": "last_name",
    "textarea_left_empty": "cover_letter",
    "partial_values": {
      "first_name": "Alex",
      "email": "alex.rivera@example.com"
    }
  },
  "lever": {
    "detect": {
      "selector": ".lever-application-form"
    },
    "required_input_left_empty": "name",
    "textarea_left_empty": "comments",
    "partial_values": {
      "email": "alex.rivera@example.com"
    }
  },
  "workday": {
    "detect": {
      "selector": "[data-automation-id=\"jobApplicationPage\"]"
    },
    "required_input_left_empty": "firstName",
    "textarea_left_empty": "additionalInformation",
    "partial_values": {
      "lastName": "Rivera",
      "email": "alex.rivera@example.com"
    }
  },
  "unknown": {
    "detect": {
      "selector": ".generic-application-form"
    },
    "required_input_left_empty": "applicant_phone",
    "textarea_left_empty": "additional_notes",
    "partial_values": {
      "applicant_name": "Alex Rivera",
      "applicant_email": "alex.rivera@example.com"
    }
  }
};
