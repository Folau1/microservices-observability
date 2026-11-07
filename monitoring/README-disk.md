# Контроль файловых систем

Конфигурация добавляет метрики и правила в существующий Telegraf/Prometheus. `kubectl apply -k monitoring` включает их вместе с остальным мониторингом; отдельный overlay не требуется. Существующая проверка старта Telegraf сохранена. Код не содержит UUID сервера, действительный marker или Kubernetes Secret: identity создаётся на узле отдельно.

Сборщик проверяет точку host mountinfo, источник и marker до расчёта объёма; при неверном/отсутствующем томе, несовпадении marker или нескольких mounts публикуется `logging_volume_mount_present=0`, а корень не принимается за том. При успешном определении монтирования, но сбое `df`, метрика наличия равна 1, метрик объёма нет. Root измеряется существующими метриками kubelet. Marker не заменяет периодическую проверку UUID и не является криптографической гарантией подлинности тома.

## Первое применение

Все команды ниже выполняются из корня **проверенной копии проекта на узле**; нужны подготовленные манифесты. До переноса изменений сохранить текущий render базового `monitoring` вне Git: путь `$backup_dir/base.yaml` понадобится для отката. Перед применением изучить diff: конфигурация изменит Telegraf и Prometheus и может кратко прервать сбор метрик.

```bash
# До замены файлов мониторинга сохранить прежний вариант.
sudo k3s kubectl config current-context
sudo k3s kubectl get nodes -o wide
free -h
df -h / /var/lib/codex-elasticsearch
findmnt -n -M /var/lib/codex-elasticsearch -o TARGET,SOURCE,FSTYPE,UUID
backup_dir=$(mktemp -d "${TMPDIR:-/tmp}/observability-rollout.XXXXXXXX")
sudo k3s kubectl kustomize monitoring > "$backup_dir/base.yaml"
echo "Манифест для отката: $backup_dir/base.yaml"

# Перенести проверенные обновления monitoring/; затем проверить тесты и render.
sudo k3s kubectl kustomize monitoring > "$backup_dir/updated.yaml"
# Код 1 у diff означает найденные изменения: их надо изучить до apply.
sudo k3s kubectl diff -f "$backup_dir/updated.yaml"
```

Перед первым apply отдельно сверить, что устройство ниже — именно существующий том логирования, а его UUID и `/etc/fstab` соответствуют друг другу. Скрипт откажется перезаписывать имеющиеся marker, Secret или identity; при прерывании возможна частичная подготовка, которую надо разобрать вручную, не удаляя данные тома.

```bash
sudo bash monitoring/telegraf/prepare-identity.sh /dev/ubuntu-vg/codex-logdata
sudo k3s kubectl apply -f "$backup_dir/updated.yaml"
sudo k3s kubectl -n observability rollout status ds/telegraf --timeout=240s
# ConfigMap с правилами сохраняет имя; чтобы Prometheus перечитал изменения:
sudo k3s kubectl -n observability rollout restart deployment/prometheus
sudo k3s kubectl -n observability rollout status deployment/prometheus --timeout=240s
```

Секрет identity и root-only файл не печатать и не добавлять в Git. Kubernetes Secret без дополнительных настроек не гарантирует шифрования на диске. Если точный source в host mountinfo изменится после миграции устройства, возникнет безопасный отказ: источник и identity необходимо пересоздать после проверки тома.

## Проверка результата

```bash
# Вывод скрипта содержит только статус и числа, не сам marker.
sudo k3s kubectl -n observability exec ds/telegraf -- /bin/sh /etc/telegraf/logging-volume/check-logging-volume.sh
sudo k3s kubectl -n observability exec deployment/prometheus -- promtool check rules /etc/prometheus/alerts.yml
telegraf_ip=$(sudo k3s kubectl -n observability get svc telegraf -o 'jsonpath={.spec.clusterIP}')
prometheus_ip=$(sudo k3s kubectl -n observability get svc prometheus -o 'jsonpath={.spec.clusterIP}')
curl -fsS "http://${telegraf_ip}:9273/metrics" | grep '^logging_volume_'
curl -fsS "http://${prometheus_ip}:9090/api/v1/targets"
curl -fsS "http://${prometheus_ip}:9090/api/v1/rules"
df -B1 / /var/lib/codex-elasticsearch
```

На исправном томе `logging_volume_mount_present=1`, а `available_bytes` и `capacity_bytes` должны соответствовать хостовому `df`. Проверить `/api/v1/rules`: наличие ConfigMap не означает, что Prometheus уже загрузил правила. При неподтверждённом источнике/маркере должно появиться состояние `LoggingVolumeMissing`, при отсутствии метрик — `LoggingVolumeMetricsMissing`, при заполнении — `LoggingVolumeLowSpace` или `RootFilesystemLowSpace`. Срабатывание и восстановление проверяются **на синтетических данных**, без отключения или заполнения рабочего тома:

```bash
cd monitoring/prometheus
promtool check rules alerts.yml
promtool test rules rules-test.yaml
cd ../telegraf
bash test-check.sh
```

Синтетические тесты показывают поведение правил Prometheus, но не подтверждают реальный инцидент на рабочем узле или доставку внешнему адресату. `for`, интервал scrape и срок хранения series Telegraf добавляют задержку. Тесты выполнены с promtool 3.15.1 и Telegraf 1.40.2; на VM проверены rollout, hostPath/Secret, соответствие чисел `df` и загрузка правил в Prometheus. Физическое заполнение или отключение рабочего тома не выполнялось. Подробности — в [отчёте](../docs/test-results/2026-11-08-disk.md).

## Откат

В том же shell, где сохранён `$backup_dir`, применить baseline и дождаться восстановления. Если shell потерян, выбрать сохранённый `base.yaml` вручную, предварительно проверив содержимое. Данные PV/PVC, marker и Secret не удаляются.

```bash
sudo k3s kubectl apply -f "$backup_dir/base.yaml"
sudo k3s kubectl -n observability rollout status ds/telegraf --timeout=240s
sudo k3s kubectl -n observability rollout restart deployment/prometheus
sudo k3s kubectl -n observability rollout status deployment/prometheus --timeout=240s
```

После отката перепроверить targets, правила и приложение. Если каталог hostPath отсутствует до запуска, Telegraf может не стартовать: в таком случае на работающем Prometheus ожидается предупреждение об отсутствии series. Непривязанный ConfigMap и identity остаются на месте; удалять их автоматически не требуется.

## Куда приходят оповещения

Эти правила меняют состояние alert в Prometheus. **Alertmanager и внешняя доставка пользователю не настроены**; если мониторинг недоступен целиком, он не сможет сообщить о собственной недоступности. Отдельный LV делит физический диск с корнем и не является резервной копией.

Документация: [Telegraf exec 1.40.2](https://raw.githubusercontent.com/influxdata/telegraf/v1.40.2/plugins/inputs/exec/sample.conf), [тестирование правил Prometheus](https://prometheus.io/docs/prometheus/latest/configuration/unit_testing_rules/).
