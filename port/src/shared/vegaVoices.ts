export const AWS_NEURAL_VOICES = [
  'Joanna', 'Matthew', 'Amy', 'Brian', 'Bianca', 'Lucia', 'Hala', 'Zayd',
  'Danielle', 'Ruth', 'Salli', 'Stephen'
] as const
export const AWS_GENERATIVE_VOICES = ['Danielle', 'Joanna', 'Ruth', 'Salli', 'Matthew', 'Stephen', 'Tiffany'] as const
export const AWS_GENERATIVE_REGIONS = [
  'us-east-1', 'us-west-2', 'eu-central-1', 'eu-west-2', 'ap-southeast-1',
  'ap-northeast-1', 'ap-northeast-2', 'ca-central-1'
] as const

export function awsVoicesForModel(modelId: string): readonly string[] {
  return modelId === 'amazon-polly-generative' ? AWS_GENERATIVE_VOICES
    : modelId === 'amazon-polly-neural' ? AWS_NEURAL_VOICES : []
}
export function speechModelLabel(modelId: string): string {
  if (modelId === 'amazon-polly-generative') return 'Natural · generative'
  if (modelId === 'amazon-polly-neural') return 'Neural'
  return modelId
}
export function awsGenerativeRegionAvailable(region: string): boolean {
  return (AWS_GENERATIVE_REGIONS as readonly string[]).includes(region)
}
