import csv
import json
import os
import math
import matplotlib.pyplot as plt
import numpy as np
from scipy.stats import pearsonr, spearmanr, kendalltau
import torch

from pytorch_pretrained_bert import GPT2Tokenizer, GPT2LMHeadModel
from pytorch_pretrained_bert import BertTokenizer, BertForMaskedLM

from configuration import CONFIG_DIR
from experiments_output import OUTPUT_DIR

CONFIG_PATH = os.path.join(CONFIG_DIR, 'config.json')
MAX_BPES_TO_SEARCH = 512


def run_lm(data, year, model_name, predictions_dict):
    """
    Using BERT or GPT2 as Language models
    :param data: The actual data of the year stored on dictionary
    :param year: The corresponding year of the data. It is used when we save the predictions
    :param model_name: Name of LM_experiments we used (BERT or GPT2). It is used on the output file name
    :param predictions_dict: A dict where we save the predictions from our experiments
    :return:
    """

    model, tokenizer, vocab_size = None, None, None
    if model_name == 'GPT2':
        tokenizer = GPT2Tokenizer.from_pretrained('gpt2')
        vocab_size = len(tokenizer.encoder)

        model = GPT2LMHeadModel.from_pretrained('gpt2')

    elif model_name == 'BERT':
        tokenizer = BertTokenizer.from_pretrained('bert-base-uncased', do_lower_case=True)
        vocab_size = len(tokenizer.vocab)

        model = BertForMaskedLM.from_pretrained('bert-base-uncased')

    model.eval()
    model.to('cuda')

    # It is used when we normalize the predicted probabilities of LM_experiments to [0, 1]
    soft_max = torch.nn.Softmax()

    # For each κ initialize a dict to store the predictions
    each_case_k_predictions = [{} for _ in range(MAX_BPES_TO_SEARCH)]

    for doc_id, doc in data.items():

        for j in range(MAX_BPES_TO_SEARCH):
            each_case_k_predictions[j].update({doc_id: {}})

        for peer_id, peer in doc['peer_summarizers'].items():
            summary = peer['system_summary']

            if not_valid(peer_id=peer_id, doc_id=doc_id):
                for j in range(MAX_BPES_TO_SEARCH):
                    each_case_k_predictions[j][doc_id].update({peer_id: vocab_size})
                continue

            indexed_summary = None
            if model_name == 'GPT2':
                indexed_summary = tokenizer.encode(summary)

            elif model_name == 'BERT':
                # BERT can handle max 512 bpes
                tokenized_summary = tokenizer.tokenize(summary)[:512]
                indexed_summary = tokenizer.convert_tokens_to_ids(tokenized_summary)

            # Convert the SUMMARY to PyTorch tensor
            tokens_tensor = torch.tensor([indexed_summary])
            tokens_tensor = tokens_tensor.to('cuda')

            with torch.no_grad():
                if summary != '':
                    if model_name == 'GPT2':
                        predictions, past = model(tokens_tensor)  # GPT returns the present

                    elif model_name == 'BERT':
                        predictions = model(tokens_tensor)  # BERT returns only the predictions

                    probability_distribution = []

                    # i --> index of the word that we are looking (i+1 the next one)
                    for i in range(predictions.shape[1] - 1):
                        # Normalize the predictions of LM_experiments by passing them through the softmax
                        soft_predictions = soft_max(predictions[0, i, :]).reshape(vocab_size)

                        if model_name == 'GPT2':
                            # GPT -> probabilities (predictions) corresponds to the next word
                            p = soft_predictions[tokens_tensor[0, i + 1]].item()

                        elif model_name == 'BERT':
                            # BERT -> probabilities (predictions) corresponds to this word which is masked
                            p = soft_predictions[tokens_tensor[0, i]].item()

                        probability_distribution.append(math.log(p, 2))

                    perplexities = get_perplexity(probabilities=probability_distribution)

                    for j in range(MAX_BPES_TO_SEARCH):
                        each_case_k_predictions[j][doc_id].update({peer_id: perplexities[j]})

                else:
                    print('BLANK')
                    for j in range(MAX_BPES_TO_SEARCH):
                        each_case_k_predictions[j][doc_id].update({peer_id: vocab_size})

    compute_correlations_of_each_k(data=data, predictions=each_case_k_predictions, model_name=model_name, year=year)


def not_valid(peer_id, doc_id):
    """
    There are some summaries full of dashes '-' which are not easy to be handled
    :param peer_id: The peer id of the author
    :param doc_id: The id of corresponding document
    :return: Bool True or False whether or not the summary is valid
    """

    return True if (peer_id == '31' and doc_id == 'D436') or (peer_id == '28' and doc_id == 'D347') else False


def get_perplexity(probabilities):
    """
    We are looking at each combination of 1,2,...512 worst bpes and calculate the perplexity
    of each one in order to decide how many of the worst bpes Ι have to take into consideration
    to approach the 'target' human metric behavior.
    :param probabilities: A list with probabilities of the next words each time
    :return: The perplexity for each combination of k = 1,2...512 worst bpes
    """

    # Sort the probabilities in order to handle them easier
    probabilities.sort(reverse=False)

    perplexities = []
    for k in range(1, MAX_BPES_TO_SEARCH + 1):
        k_worst_probabilities = probabilities[:k]
        mean_of_probabilities = np.mean(np.array(k_worst_probabilities))
        perplexity = math.pow(2, -mean_of_probabilities)
        perplexities.append(perplexity)

    return perplexities


def compute_correlations_of_each_k(data, predictions, model_name, year):
    """
    Computes the correlations between the BERT or GPT2 Language Model with Q1
    :param data: The actual data of the year stored on dictionary
    :param predictions: A dict where the predictions from our experiments are saved
    :param model_name: Name of LM_experiments we used (BERT or GPT2). It is used on the output file name
    :param year: The corresponding year of the data
    :return: The k which achieved the best (spearman) correlations
    """
    system_ids = {peer_id for doc in data.values() for peer_id, peer in doc['peer_summarizers'].items()}

    q1_aggregation_table = np.zeros(len(system_ids))
    model_predictions_aggregation_table = []

    # len(predictions) = MAX_BPES_TO_SEARCH
    for i in range(len(predictions)):
        model_predictions_aggregation_table.append(np.zeros(len(system_ids)))

    for i, sid in enumerate(system_ids):
        q1_scores = []
        model_scores = [[] for _ in range(len(predictions))]

        for doc_id, doc in data.items():
            q1_scores.append(doc['peer_summarizers'][sid]['human_scores']['Q1'])
            q1_aggregation_table[i] = np.mean(np.array(q1_scores))

            for j in range(len(predictions)):
                model_scores[j].append(predictions[j][doc_id][sid])

            for k in range(len(predictions)):
                model_predictions_aggregation_table[k][i] = np.mean(np.array(model_scores[k]))

    spearman, kendall, pearson, lines_2_write = [], [], [], []
    for k in range(len(predictions)):
        spearman_corr = spearmanr(q1_aggregation_table, -model_predictions_aggregation_table[k])[0]
        spearman.append(spearman_corr)
        kendall_corr = kendalltau(q1_aggregation_table, -model_predictions_aggregation_table[k])[0]
        kendall.append(kendall_corr)
        pearson_corr = pearsonr(q1_aggregation_table, -model_predictions_aggregation_table[k])[0]
        pearson.append(pearson_corr)

        lines_2_write.append([str(k+1) + ' bpes', spearman_corr, kendall_corr, pearson_corr])

    path_to_save = os.path.join(OUTPUT_DIR, 'Q1 - {0:s}  {1:s}.csv'.format(model_name, year))

    with open(path_to_save, 'w') as file:
        the_writer = csv.writer(file, delimiter=',')
        the_writer.writerow(['# Bpes', 'Spearman', 'Kendall', 'Pearson'])

        for line in lines_2_write:
            the_writer.writerow(line)

    # Visualize the correlations of each k-worst bpes perplexity and actual-scores
    visualize_correlation_metrics(spearman, kendall, pearson, model_name, year)

    spearman_max = max(spearman)
    best_k = spearman.index(spearman_max)
    print('TRAIN best_k: {}'.format(best_k))


def visualize_correlation_metrics(spearman_scores, kendall_scores, pearson_scores, model_name, year):
    """
    Visualize the correlation metric from all the k-worst bpes cases
    :param spearman_scores: The spearman correlations of Q1 and k-worst bpes each time
    :param kendall_scores: The Kendall correlations of Q1 and k-worst bpes each time
    :param pearson_scores: The Pearson correlations of Q1 and k-worst bpes each time
    :param model_name: Name of LM_experiments we used (BERT or GPT2). It is used on the output file name
    :param year: The corresponding year of the data
    """
    x_ticks = [i for i in range(1, MAX_BPES_TO_SEARCH + 1)]
    plt.figure(figsize=(26, 8))

    y_max = max(spearman_scores)
    x_pos = spearman_scores.index(y_max)
    x_max = x_ticks[x_pos]
    print('VISUALIZE x_pos: {}  x_max: {}'.format(x_pos, x_max))

    plt.subplot(1, 3, 1)
    plt.plot(x_ticks, spearman_scores, 'bo')
    plt.annotate('{0:.2f} K={1:d}'.format(y_max, x_max), xy=(x_max, y_max), xytext=(x_max, y_max+0.005))
    plt.title('Spearman')
    plt.xlabel('# of worst words')

    y_max = max(kendall_scores)
    x_pos = kendall_scores.index(y_max)
    x_max = x_ticks[x_pos]

    plt.subplot(1, 3, 2)
    plt.plot(x_ticks, kendall_scores, 'bo')
    plt.annotate('{0:.2f} K={1:d}'.format(y_max, x_max), xy=(x_max, y_max), xytext=(x_max, y_max + 0.005))
    plt.title('Kendall')
    plt.xlabel('# of worst words')

    y_max = max(pearson_scores)
    x_pos = pearson_scores.index(y_max)
    x_max = x_ticks[x_pos]

    plt.subplot(1, 3, 3)
    plt.plot(x_ticks, pearson_scores, 'bo')
    plt.annotate('{0:.2f} K={1:d}'.format(y_max, x_max), xy=(x_max, x_max), xytext=(x_max, y_max + 0.005))
    plt.title('Pearson')
    plt.xlabel('# of worst words')

    path_to_save = os.path.join(OUTPUT_DIR, 'Q1 - {0:s}  {1:s}.png'.format(model_name, year))
    plt.savefig(path_to_save)
    plt.show()