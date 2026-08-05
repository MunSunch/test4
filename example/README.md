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
    config_103.yml
    config_104.yml
    config_105.yml
  affects/
    affect_enable_103.yml
    affect_disable_103.yml
    affect_enable_104.yml
    affect_disable_104.yml
    affect_enable_105.yml
    affect_disable_105.yml
  config.json
```

`--dir example` указывает на корень, а `configs/` и `affects/` прописаны в паттернах
внутри `config.json`. Номер в имени файла связывает их в один эксперимент:
`config_103.yml` + `affect_enable_103.yml` + `affect_disable_103.yml`.

Содержимое yml-файлов ничего не значит — программа их не читает, а только передаёт
абсолютные пути в аргументы утилиты.

## Какие команды получаются

Для N=103 из `/Users/munir/PythonProject1/example`:

```
create    -c .../configs/config_103.yml -f .../affects/affect_enable_103.yml  -e
enable    -c .../configs/config_103.yml -u -f .../affects/affect_enable_103.yml  -e
disable   -c .../configs/config_103.yml -u -f .../affects/affect_disable_103.yml -e
stop      -c .../configs/config_103.yml
```

Посмотреть их целиком, ничего не запуская: `--dry-run`.

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
