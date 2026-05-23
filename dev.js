// Вставьте в консоль браузера для отслеживания работы обработчика
(function traceSpeakingStatusHandler() {
    console.log('🔍 Отслеживание SpeakingStatusHandler...');

    // Получаем экземпляр обработчика
    let handler = null;

    // Ищем в глобальных объектах
    for (const key in window) {
        if (window[key] && window[key].constructor && window[key].constructor.name === 'SpeakingStatusHandler') {
            handler = window[key];
            console.log('✅ Найден SpeakingStatusHandler:', handler);
            break;
        }
    }

    // Если не нашли, создаем обертку для store.dispatch
    const store = globalThis.store;
    if (store) {
        const originalDispatch = store.dispatch;
        store.dispatch = function(action, payload) {
            if (action === 'setSpeaking' || action === 'participantsStore/setSpeaking') {
                const stack = new Error().stack;
                console.group(`🎯 SPEAKING UPDATE от ${payload?.speaking ? 'НАЧАЛО' : 'ОКОНЧАНИЕ'}`);
                console.log('Время:', new Date().toLocaleTimeString());
                console.log('AttendeeId:', payload?.attendeeId);
                console.log('Speaking:', payload?.speaking);
                console.log('Stack trace:');
                console.log(stack.split('\n').slice(1, 8).join('\n'));
                console.groupEnd();
            }
            return originalDispatch.call(this, action, payload);
        };

        // Отслеживаем модели
        setTimeout(() => {
            // Проверяем localMediaModel
            if (window.localMediaModel) {
                console.log('📱 localMediaModel найден');
                const originalSpeaking = window.localMediaModel.attributes.speaking;
                Object.defineProperty(window.localMediaModel.attributes, 'speaking', {
                    get: () => originalSpeaking,
                    set: (value) => {
                        console.log(`🎤 Локальный speaking изменился: ${originalSpeaking} → ${value}`);
                        originalSpeaking = value;
                    }
                });
            }

            // Проверяем callParticipantCollection
            if (window.callParticipantCollection) {
                console.log('👥 callParticipantCollection найден');
                const models = window.callParticipantCollection.callParticipantModels;
                models.forEach((model, index) => {
                    const originalSpeaking = model.attributes.speaking;
                    Object.defineProperty(model.attributes, 'speaking', {
                        get: () => originalSpeaking,
                        set: (value) => {
                            console.log(`🗣️ Участник ${model.attributes.name}: speaking ${originalSpeaking} → ${value}`);
                            originalSpeaking = value;
                        }
                    });
                });
            }
        }, 1000);
    }

    console.log('✅ Отслеживание запущено! Смотрите консоль.');

    return {
        stop: () => {
            if (store) store.dispatch = originalDispatch;
            console.log('⏹️ Отслеживание остановлено');
        }
    };
})();


// В консоли браузера - показать всех слушателей speaking событий
function showSpeakingListeners() {
    // Показать localMediaModel слушатели
    if (window.localMediaModel) {
        console.log('localMediaModel события:',
            window.localMediaModel._events?.['change:speaking'],
            window.localMediaModel._events?.['change:stoppedSpeaking']
        );
    }

    // Показать участников и их speaking статус
    if (window.callParticipantCollection) {
        window.callParticipantCollection.callParticipantModels.forEach(model => {
            console.log(`${model.attributes.name}: speaking=${model.attributes.speaking}`);
        });
    }

    // Показать текущие speaking данные из store
    if (globalThis.store?.state?.participantsStore?.speaking) {
        console.log('Store speaking данные:',
            globalThis.store.state.participantsStore.speaking
        );
    }
}

showSpeakingListeners();