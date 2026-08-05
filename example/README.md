# Готовый пример

Работает из коробки: вместо `storm` вызывается заглушка `tests/fake_storm.py`,
поэтому весь прогон занимает пару секунд, а не сорок минут.

```bash
cd ..                                                          # в корень проекта
python3 runner.py --dir example --config example/config.json --mode all --dry-run
python3 runner.py --dir example --config example/config.json --mode all
```

Результаты появятся в `storm_runs/` рядом с `runner.py`.

## Что здесь лежит

```
example/
  configs/
    config_103m4.yml
    config_103m5.yml
    config_104m5.yml
  affects/
    affect_enable_103m4.yml
    affect_disable_103m4.yml
    affect_enable_103m5.yml
    affect_disable_103m5.yml
    affect_enable_104m5.yml
    affect_disable_104m5.yml
  config.json
```

`--dir example` указывает на корень, а `configs/` и `affects/` прописаны в паттернах
внутри `config.json`. Идентификатор в имени файла связывает их в один эксперимент:
`config_103m5.yml` + `affect_enable_103m5.yml` + `affect_disable_103m5.yml`.

Здесь три эксперимента, и номер 103 намеренно встречается дважды — на кластерах
m4 и m5. Это два разных эксперимента: у них свои файлы, свои папки с логами
и свои записи в журнале.

Содержимое yml-файлов ничего не значит — программа их не читает, а только передаёт
абсолютные пути в аргументы утилиты.

## Какие команды получаются

Для 103m5 из `/Users/munir/PythonProject1/example`:

```
create    -c .../configs/config_103m5.yml -f .../affects/affect_enable_103m5.yml  -e
enable    -c .../configs/config_103m5.yml -u -f .../affects/affect_enable_103m5.yml  -e
disable   -c .../configs/config_103m5.yml -u -f .../affects/affect_disable_103m5.yml -e
stop      -c .../configs/config_103m5.yml
```

Посмотреть их целиком, ничего не запуская: `--dry-run`.

Выбрать только один кластер: `--only 103m5`. Взять номер на всех кластерах
сразу: `--only 103`.

## Как перевести на настоящую утилиту

В `config.json` две правки:

```diff
- "exe": "python3",
+ "exe": "storm",

- "args": ["{root}/tests/fake_storm.py", "-c", "{config}", "-f", "{enable}", "-e"],
+ "args": ["-c", "{config}", "-f", "{enable}", "-e"],
```

`{root}` это папка, где лежит `runner.py` — он нужен только для того, чтобы пример
находил заглушку на любой машине. Настоящей команде он не нужен.

Дальше — `--dir` на свою папку с экспериментами и `--workers 16`.
