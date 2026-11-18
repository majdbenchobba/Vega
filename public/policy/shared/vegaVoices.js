export const AWS_NEURAL_VOICES = [
  'Joanna', 'Matthew', 'Amy', 'Brian', 'Bianca', 'Lucia', 'Hala', 'Zayd',
  'Danielle', 'Ruth', 'Salli', 'Stephen'
]         
export const AWS_GENERATIVE_VOICES = ['Danielle', 'Joanna', 'Ruth', 'Salli', 'Matthew', 'Stephen', 'Tiffany']         
export const AWS_GENERATIVE_REGIONS = [
  'us-east-1', 'us-west-2', 'eu-central-1', 'eu-west-2', 'ap-southeast-1',
  'ap-northeast-1', 'ap-northeast-2', 'ca-central-1'
]         

export function awsVoicesForModel(modelId        )                    {
  return modelId === 'amazon-polly-generative' ? AWS_GENERATIVE_VOICES
    : modelId === 'amazon-polly-neural' ? AWS_NEURAL_VOICES : []
}
export function speechModelLabel(modelId        )         {
  if (modelId === 'amazon-polly-generative') return 'Natural · generative'
  if (modelId === 'amazon-polly-neural') return 'Neural'
  return modelId
}
export function awsGenerativeRegionAvailable(region        )          {
  return (AWS_GENERATIVE_REGIONS                     ).includes(region)
}
