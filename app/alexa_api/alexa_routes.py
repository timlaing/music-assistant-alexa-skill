"""Route definitions for alexa_api (alexa_routes)."""

import json
from pathlib import Path

import shared_store
from app_settings import get_setting
from flask import jsonify, request


def register_routes(bp):
    @bp.route('/push-url', methods=['POST'])
    def push_url():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'Expected a JSON object'}), 400
        stream_url = data.get('streamUrl')
        if not isinstance(stream_url, str) or not stream_url:
            return jsonify({'error': 'Missing required fields'}), 400

        shared_store.set_alexa({
            'streamUrl': stream_url,
            'title': data.get('title'),
            'secondary': data.get('secondary'),
            'imageUrl': data.get('imageUrl'),
        })
        return jsonify({'status': 'ok'})

    @bp.route('/latest-url', methods=['GET'])
    def latest_url():
        payload = shared_store.get_alexa()
        if not payload:
            return jsonify({'error': 'Check skill invocations and skill logs.  If there are no invocations, you have made a configuration error'}), 404
        return jsonify(payload)
    
    @bp.route('/intents', methods=['GET'])
    def intents():
        locale = get_setting('LOCALE', 'en-US')
        intents_file = Path(__file__).parent.parent / 'models' / 'built-in' / f'{locale}.json'
        try:
            with open(intents_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                language_model = data.get('interactionModel', {}).get('languageModel', {})
                invocation_name = language_model.get('invocationName')
                intent_list = language_model.get('intents', [])
                intents_with_utterances = [
                    {
                        'intent': intent.get('intent'),
                        'utterances': intent.get('utterances', [])
                    }
                    for intent in intent_list if intent.get('intent')
                ]
                return jsonify({
                    'locale': locale,
                    'invocationName': invocation_name,
                    'intents': intents_with_utterances
                })
        except FileNotFoundError:
            return jsonify({
                'error': f'Intents file for locale {locale} not found',
                'locale': locale,
                'invocationName': None,
                'intents': []
            }), 404
        except Exception as e:
            return jsonify({
                'error': f'Error loading intents: {str(e)}',
                'locale': locale,
                'invocationName': None,
                'intents': []
            }), 500
