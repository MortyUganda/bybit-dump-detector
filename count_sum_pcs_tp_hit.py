import json

with open('auto_shorts_20260508_122628.json') as json_file:
    data = json.load(json_file)
    data_filtered = [el for el in data if el['close_reason']=='tp_hit']
    print(data_filtered)
    print('Прибыльных сделок: ', len(data_filtered))
    print('Всего сделок: ', len(data))