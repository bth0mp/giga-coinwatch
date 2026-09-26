"""Human-readable monitoring support, separate from the latest scan outcome."""

SUPPORT_LABELS = {
    'ready': 'Ready to enable',
    'access_blocked': 'Access blocked',
    'permission_required': 'Permission needed',
    'unreachable': 'Unavailable',
    'login_required': 'Login required',
    'no_stock': 'No current stock',
    'not_applicable': 'Outside coin scope',
    'needs_adapter': 'Parser needed',
    'needs_review': 'Needs review',
}


def describe_support(source):
    if source.get('adapter'):
        return {'key': 'active' if source.get('enabled') else 'ready',
                'label': 'Active' if source.get('enabled') else SUPPORT_LABELS['ready']}
    status = source.get('support_status', 'needs_review')
    if status not in SUPPORT_LABELS or status == 'ready':
        status = 'needs_review'
    return {'key': status, 'label': SUPPORT_LABELS[status]}
